import logging

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

import models  # noqa: F401  - registers every table on Base.metadata

from routes import auth, projects, public
from services import attestation_service

logger = logging.getLogger(__name__)

app = FastAPI(title="SkillChain API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(projects.router, prefix="/projects", tags=["Projects"])
app.include_router(public.router, prefix="/public", tags=["Public"])

@app.get("/")
def root():
    return {"status": "SkillChain API is running"}


@app.get("/.well-known/skillchain-signing-key", response_class=PlainTextResponse)
def signing_key():
    """The attestation log's public key, in OpenSSH allowed_signers form.

    Mounted at the root rather than under /public because the point of a
    well-known URI is that a verifier can guess it from the domain alone and
    run `git verify-commit` without asking anyone for the key first.
    """
    try:
        allowed_signers = attestation_service.from_env().allowed_signers()
    except attestation_service.AttestationError as exc:
        # Unconfigured is not an error on the caller's part, and a 500 would
        # suggest the key exists and the server is broken.
        logger.warning("signing key requested but attestation is not configured: %s", exc)
        allowed_signers = None
    if not allowed_signers:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "This deployment publishes no signing key")
    return allowed_signers
