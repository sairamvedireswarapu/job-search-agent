"""FastAPI routes."""
import asyncio
import base64
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import pypdf
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from google.cloud import firestore, kms
from pydantic import BaseModel

from agents.orchestrator import run_job_search
from auth.google_oauth import (
    build_auth_url,
    create_session,
    exchange_code_for_tokens,
    get_current_user,
    verify_google_id_token,
)
from config import get_settings

settings = get_settings()
router = APIRouter()
logger = logging.getLogger(__name__)

db = firestore.Client()
kms_client = kms.KeyManagementServiceClient()


# ── Helpers ────────────────────────────────────────────────────────────────

def _encrypt(plaintext: str) -> str:
    response = kms_client.encrypt(
        request={"name": settings.KMS_KEY_NAME, "plaintext": plaintext.encode()}
    )
    return base64.b64encode(response.ciphertext).decode()


def _decrypt(ciphertext_b64: str) -> str:
    ciphertext = base64.b64decode(ciphertext_b64)
    response = kms_client.decrypt(
        request={"name": settings.KMS_KEY_NAME, "ciphertext": ciphertext}
    )
    return response.plaintext.decode()


def _extract_pdf_text(data: bytes) -> str:
    import io
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


async def _get_user_api_keys(user_id: str) -> dict:
    doc = db.collection("user_api_keys").document(user_id).get()
    if not doc.exists:
        return {}
    raw = doc.to_dict()
    keys = {}
    for field in ["jsearch_key", "adzuna_app_id", "adzuna_api_key", "gemini_key"]:
        if raw.get(field):
            try:
                keys[field] = _decrypt(raw[field])
            except Exception:
                logger.warning("Failed to decrypt %s for user %s", field, user_id)
    return keys


async def _update_run_status(run_id: str, user_id: str, status: str):
    db.collection("run_history").document(run_id).set(
        {"status": status, "updated_at": datetime.now(tz=timezone.utc)},
        merge=True,
    )


# ── Auth routes ────────────────────────────────────────────────────────────

@router.get("/auth/google")
async def auth_google_start():
    state = secrets.token_urlsafe(16)
    return {"url": build_auth_url(state)}


@router.get("/auth/google/callback")
async def auth_google_callback(code: str, state: str):
    tokens = await exchange_code_for_tokens(code)
    claims = verify_google_id_token(tokens["id_token"])

    user_id = claims["sub"]
    email = claims.get("email", "")
    name = claims.get("name", "")

    # Upsert user in Firestore
    db.collection("users").document(user_id).set(
        {"user_id": user_id, "email": email, "name": name},
        merge=True,
    )

    session_token = create_session(user_id, email, name)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie("session_token", session_token, httponly=True, samesite="lax", max_age=604800)
    return response 


@router.post("/auth/logout")
async def logout(user: dict = Depends(get_current_user)):
    return {"ok": True}


# ── Profile routes ─────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    doc = db.collection("users").document(user["user_id"]).get()
    if not doc.exists:
        raise HTTPException(404, "User not found")
    data = doc.to_dict()
    return {
        "user_id": data["user_id"],
        "email": data.get("email"),
        "name": data.get("name"),
        "has_cv": bool(data.get("cv_text")),
        "last_run_at": data.get("last_run_at"),
    }


@router.post("/me/cv")
async def upload_cv(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    data = await file.read()
    cv_text = _extract_pdf_text(data)
    if len(cv_text) < 100:
        raise HTTPException(400, "Could not extract text from PDF — ensure it is not a scanned image")

    db.collection("users").document(user["user_id"]).set(
        {"cv_text": cv_text, "cv_updated_at": datetime.now(tz=timezone.utc)},
        merge=True,
    )
    return {"ok": True, "char_count": len(cv_text)}


class ApiKeysPayload(BaseModel):
    jsearch_key: Optional[str] = None
    adzuna_app_id: Optional[str] = None
    adzuna_api_key: Optional[str] = None
    gemini_key: Optional[str] = None


@router.post("/me/api-keys")
async def save_api_keys(payload: ApiKeysPayload, user: dict = Depends(get_current_user)):
    encrypted = {}
    for field, value in payload.model_dump().items():
        if value:
            encrypted[field] = _encrypt(value)
    encrypted["updated_at"] = datetime.now(tz=timezone.utc)
    db.collection("user_api_keys").document(user["user_id"]).set(encrypted, merge=True)
    return {"ok": True}


# ── Filter presets ─────────────────────────────────────────────────────────

@router.get("/me/presets")
async def list_presets(user: dict = Depends(get_current_user)):
    docs = db.collection("filter_presets").where("user_id", "==", user["user_id"]).stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


class PresetPayload(BaseModel):
    preset_name: str
    keywords: list[str]
    countries: list[str]
    job_types: Optional[list[str]] = None
    work_modes: Optional[list[str]] = None
    posted_within_days: int = 7
    min_score: int = 40
    max_results: int = 20


@router.post("/me/presets")
async def save_preset(payload: PresetPayload, user: dict = Depends(get_current_user)):
    doc_ref = db.collection("filter_presets").document()
    doc_ref.set({"user_id": user["user_id"], **payload.model_dump()})
    return {"ok": True, "id": doc_ref.id}


@router.delete("/me/presets/{preset_id}")
async def delete_preset(preset_id: str, user: dict = Depends(get_current_user)):
    doc = db.collection("filter_presets").document(preset_id).get()
    if not doc.exists or doc.to_dict().get("user_id") != user["user_id"]:
        raise HTTPException(404, "Preset not found")
    db.collection("filter_presets").document(preset_id).delete()
    return {"ok": True}


# ── Run routes ─────────────────────────────────────────────────────────────

class RunPayload(BaseModel):
    keywords: list[str]
    countries: list[str]
    model: str = "haiku"
    job_types: Optional[list[str]] = None
    work_modes: Optional[list[str]] = None
    posted_within_days: int = 7
    min_score: int = 40
    max_results: int = 20


@router.post("/run")
async def trigger_run(payload: RunPayload, user: dict = Depends(get_current_user)):
    # Validate
    if len(payload.keywords) > 5:
        raise HTTPException(400, "Max 5 keywords per run")
    if len(payload.countries) > 5:
        raise HTTPException(400, "Max 5 countries per run")

    # Load user data
    user_doc = db.collection("users").document(user["user_id"]).get()
    if not user_doc.exists:
        raise HTTPException(404, "User profile not found")
    user_data = user_doc.to_dict()
    cv_text = user_data.get("cv_text")
    if not cv_text:
        raise HTTPException(400, "Please upload your CV before running a search")

    api_keys = await _get_user_api_keys(user["user_id"])
    if not api_keys.get("gemini_key"):
        raise HTTPException(400, "Gemini API key is required — add it in your profile")

    run_id = uuid.uuid4().hex[:12]

    # Write initial run record
    db.collection("run_history").document(run_id).set({
        "run_id": run_id,
        "user_id": user["user_id"],
        "status": "running",
        "filters_used": payload.model_dump(),
        "created_at": datetime.now(tz=timezone.utc),
    })

    # Update last_run_at
    db.collection("users").document(user["user_id"]).set(
        {"last_run_at": datetime.now(tz=timezone.utc)}, merge=True
    )

    async def _status_callback(status: str):
        await _update_run_status(run_id, user["user_id"], status)

    # Fire-and-forget in background
    async def _run():
        try:
            result = await run_job_search(
                user_id=user["user_id"],
                cv_text=cv_text,
                keywords=payload.keywords,
                countries=payload.countries,
                api_keys=api_keys,
                model=payload.model,
                job_types=payload.job_types,
                work_modes=payload.work_modes,
                posted_within_days=payload.posted_within_days,
                min_score=payload.min_score,
                max_results=payload.max_results,
                on_status=_status_callback,
            )
            db.collection("run_history").document(run_id).set({
                "status": "done",
                "gcs_path": result.get("gcs_path"),
                "signed_url": result.get("signed_url"),
                "total_raw_jobs": result.get("total_raw_jobs"),
                "after_dedup": result.get("after_dedup"),
                "after_score_filter": result.get("after_score_filter"),
                "completed_at": datetime.now(tz=timezone.utc),
            }, merge=True)
        except Exception as e:
            logger.error("Run %s failed: %s", run_id, e)
            db.collection("run_history").document(run_id).set({
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now(tz=timezone.utc),
            }, merge=True)

    asyncio.create_task(_run())
    return {"run_id": run_id, "status": "running"}


@router.get("/run/{run_id}/status")
async def get_run_status(run_id: str, user: dict = Depends(get_current_user)):
    doc = db.collection("run_history").document(run_id).get()
    if not doc.exists:
        raise HTTPException(404, "Run not found")
    data = doc.to_dict()
    if data.get("user_id") != user["user_id"]:
        raise HTTPException(403, "Forbidden")
    return data


@router.get("/run/{run_id}/download")
async def get_download_url(run_id: str, user: dict = Depends(get_current_user)):
    doc = db.collection("run_history").document(run_id).get()
    if not doc.exists:
        raise HTTPException(404, "Run not found")
    data = doc.to_dict()
    if data.get("user_id") != user["user_id"]:
        raise HTTPException(403, "Forbidden")
    signed_url = data.get("signed_url")
    if not signed_url:
        raise HTTPException(400, "Download not ready yet")
    return {"signed_url": signed_url}


@router.get("/runs")
async def list_runs(user: dict = Depends(get_current_user)):
    docs = (
        db.collection("run_history")
        .where("user_id", "==", user["user_id"])
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(20)
        .stream()
    )
    return [{"id": d.id, **d.to_dict()} for d in docs]
