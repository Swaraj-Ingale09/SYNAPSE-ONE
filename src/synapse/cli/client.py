"""Typed HTTP client for the local gateway.

Every CLI command talks to the core through this client — the CLI holds no
intelligence of its own (spec section 2: clients of one persistent service).
"""

from __future__ import annotations

from typing import Any

import httpx

from synapse.config import Settings
from synapse.contracts import Job, JobCreate, JobDetail, JobEvent, JobStatus


class CoreNotRunning(RuntimeError):
    """The core gateway could not be reached."""


class ApiError(RuntimeError):
    """The gateway answered with an error status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class SynapseClient:
    def __init__(self, settings: Settings, timeout: float = 10.0) -> None:
        self.settings = settings
        headers: dict[str, str] = {}
        if settings.token:
            headers["Authorization"] = f"Bearer {settings.token}"
        self._http = httpx.Client(
            base_url=settings.api_url, headers=headers, timeout=timeout
        )

    def __enter__(self) -> SynapseClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._http.close()

    def close(self) -> None:
        self._http.close()

    # --- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CoreNotRunning(
                f"cannot reach the SYNAPSE core at {self.settings.api_url}"
            ) from exc
        if response.status_code >= 400:
            detail = f"HTTP {response.status_code}"
            try:
                body = response.json()
                if isinstance(body, dict) and "detail" in body:
                    detail = str(body["detail"])
            except ValueError:
                detail = response.text[:200] or detail
            raise ApiError(response.status_code, detail)
        return response

    # --- reads ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def ready(self) -> dict[str, Any]:
        """Readiness probe; a 503 payload is returned as data, not an error."""
        try:
            response = self._http.get("/ready")
        except httpx.HTTPError as exc:
            raise CoreNotRunning(
                f"cannot reach the SYNAPSE core at {self.settings.api_url}"
            ) from exc
        try:
            return response.json()
        except ValueError:
            return {"status": "unknown", "database": "unknown"}

    def system_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/system/status").json()

    def get_job(self, job_id: str) -> JobDetail:
        data = self._request("GET", f"/v1/jobs/{job_id}").json()
        return JobDetail.model_validate(data)

    def list_jobs(
        self,
        status: JobStatus | None = None,
        project_scope: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status.value
        if project_scope:
            params["project_scope"] = project_scope
        data = self._request("GET", "/v1/jobs", params=params).json()
        return [Job.model_validate(item) for item in data]

    def job_events(self, job_id: str, limit: int = 500) -> list[JobEvent]:
        data = self._request(
            "GET", f"/v1/jobs/{job_id}/events", params={"limit": limit}
        ).json()
        return [JobEvent.model_validate(item) for item in data]

    def events_since(self, since_id: int = 0, limit: int = 100) -> list[JobEvent]:
        data = self._request(
            "GET",
            "/v1/events",
            params={"since_id": since_id, "limit": limit},
        ).json()
        return [JobEvent.model_validate(item) for item in data]

    # --- writes -----------------------------------------------------------

    def create_job(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        project_scope: str | None = None,
    ) -> Job:
        body = JobCreate(task_type=task_type, payload=payload or {}, project_scope=project_scope)
        data = self._request("POST", "/v1/jobs", json=body.model_dump(mode="json")).json()
        return Job.model_validate(data)

    def cancel_job(self, job_id: str) -> Job:
        data = self._request("POST", f"/v1/jobs/{job_id}/cancel").json()
        return Job.model_validate(data)

    def shutdown(self) -> dict[str, Any]:
        return self._request("POST", "/v1/admin/shutdown").json()
