from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All values sourced from env vars, per decision log's two-tier secret
    model: static app secrets never live in code or Git.

    Concretely, per environment:
      - Local dev: values live in a git-ignored .env file at the project
        root (see .gitignore -- .env is already excluded). .env.example is
        the checked-in template with placeholder values only.
      - Deployed (Railway/Render): the same variable names are set directly
        in the platform's dashboard/env var UI. Never derived from a
        committed file, never copy-pasted through a shell history that
        might get logged.
    JWT_SECRET_KEY and FERNET_MASTER_KEY are the two that matter most here --
    both must be generated fresh per environment (dev's are throwaway;
    deployed ones are the real secrets) and never reused between the two.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str

    # JWT
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Fernet (encrypts tenants.llm_api_key_ref)
    fernet_master_key: str

    @field_validator("fernet_master_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        # Fail fast at startup with a clear message, rather than a confusing
        # crash the first time app/crypto.py tries to encrypt something --
        # e.g. if someone deploys with .env.example's literal placeholder.
        try:
            Fernet(v.encode())
        except Exception as e:
            raise ValueError(
                "FERNET_MASTER_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
        return v

    # No app-level generation-provider settings live here on purpose.
    # GuardRail owns no generation model of any kind -- every provider/model/
    # key is per-tenant (tenants.llm_*) or per-user (user_llm_config.llm_*),
    # resolved at request time, never sourced from this app's own env vars.
    # (Prior revision had default_llm_provider/default_groq_* here as a
    # leftover from the pre-correction architecture -- removed, not renamed.)

    # Embedding provider — self-hosted, not the HuggingFace Inference API.
    # Corrected this phase: the shipped scaffolding (this file, .env.example,
    # requirements.txt) still described an external Inference-API call with
    # an hf_api_token, but the decision log records that as resolved the
    # other way — every ingested chunk and every query's text would
    # otherwise go to a third party in plaintext, on 100% of ingested
    # content, not just what a query's RBAC/similarity filter surfaces.
    # bge-small-en-v1.5 was the name written down under the old Inference-API
    # framing; the model-selection follow-up landed on nomic-embed-text-v1.5
    # instead (768 native dims, MIT-licensed, CPU-viable), loaded in-process
    # via sentence-transformers — see app/embeddings.py. No token needed:
    # nothing leaves this process to fetch an embedding.
    embedding_model_name: str = "nomic-ai/nomic-embed-text-v1.5"

    # Ingestion (Phase 4). Synchronous by design this phase (decision log,
    # Session 5 addendum #4) — a cap keeps one oversized upload from tying up
    # a request/worker indefinitely under that model. Not a figure from the
    # source docs; a reasonable default flagged here rather than picked
    # silently. Revisit alongside the sync-vs-async question itself if real
    # uploads turn out to need more.
    ingest_max_file_size_mb: int = 20

    # Minimal send-only email (decision log Phase 3 item #4: new Admin's
    # random password, subject+body only, NOT the full reset flow).
    # smtp_host empty (the default) means "dev mode" -- app/email.py logs
    # instead of sending over the network. Set all four on deploy to send
    # for real.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@guardrail.local"


settings = Settings()
