# tests/image/test_image_job_idempotency.py
"""
배치 이미지 생성 API(/api/v2/images/generate/batch)의 job_id 중복 방지 테스트.

실제 PostgreSQL(Testcontainers)에 ai_image_job_tracking 테이블을 만들고,
이미지 서버·스토리지·BE 콜백·백그라운드 작업은 가짜 객체로 대체합니다.
Docker가 실행 중이어야 합니다.
"""
import asyncio

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer

from app import dependencies
from app.api.v2 import image_endpoints
from app.services.postgresql_service import PostgreSQLService

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS ai_image_job_tracking (
    job_id     VARCHAR PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

CONCURRENT_REQUESTS = 10


class FakeImageService:
    """헬스체크만 흉내 내는 이미지 서비스. 실제 헬스체크처럼 네트워크 대기가 있습니다."""

    def __init__(self, healthy: bool = True, latency: float = 0.05):
        self.healthy = healthy
        self.latency = latency

    async def check_health(self) -> bool:
        await asyncio.sleep(self.latency)
        return self.healthy


@pytest.fixture(scope="session")
def postgres_dsn():
    with PostgresContainer("postgres:16-alpine", driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
async def pg_service(postgres_dsn):
    service = PostgreSQLService(use_ssh=False)
    service.pool = await asyncpg.create_pool(postgres_dsn, min_size=CONCURRENT_REQUESTS, max_size=CONCURRENT_REQUESTS)
    await service.execute(CREATE_TRACKING_TABLE)
    await service.execute("TRUNCATE ai_image_job_tracking")
    yield service
    await service.pool.close()


@pytest.fixture
def scheduled_jobs(monkeypatch):
    """백그라운드 작업 대신 예약된 job_id만 기록합니다."""
    jobs = []

    async def fake_generate_images_in_background(payload, **kwargs):
        jobs.append(payload.id)

    monkeypatch.setattr(image_endpoints, "generate_images_in_background", fake_generate_images_in_background)
    return jobs


def build_client(pg_service, image_service) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(image_endpoints.router, prefix="/api/v2")
    app.dependency_overrides = {
        dependencies.get_postgresql_service: lambda: pg_service,
        dependencies.get_image_service: lambda: image_service,
        dependencies.get_storage_service: lambda: object(),
        dependencies.get_backend_apiclient: lambda: object(),
    }
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def batch_payload(job_id: str) -> dict:
    return {
        "id": job_id,
        "imagePrompts": [
            {"seed": 1, "width": 1024, "height": 1024, "prompt": "p", "panel_id": i, "negative_prompt": "n"}
            for i in range(5)
        ],
    }


async def test_concurrent_duplicate_requests_accept_only_one(pg_service, scheduled_jobs):
    async with build_client(pg_service, FakeImageService()) as client:
        responses = await asyncio.gather(*[
            client.post("/api/v2/images/generate/batch", json=batch_payload("job-1"))
            for _ in range(CONCURRENT_REQUESTS)
        ])

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(202) == 1, f"수락된 요청 수: {status_codes.count(202)}/{CONCURRENT_REQUESTS}"
    assert status_codes.count(409) == CONCURRENT_REQUESTS - 1
    assert scheduled_jobs == ["job-1"]


async def test_unhealthy_server_returns_503_and_allows_retry(pg_service, scheduled_jobs):
    image_service = FakeImageService(healthy=False)
    async with build_client(pg_service, image_service) as client:
        unhealthy = await client.post("/api/v2/images/generate/batch", json=batch_payload("job-2"))
        image_service.healthy = True
        retried = await client.post("/api/v2/images/generate/batch", json=batch_payload("job-2"))

    assert unhealthy.status_code == 503
    assert retried.status_code == 202
    assert scheduled_jobs == ["job-2"]


async def test_failed_job_can_be_requested_again_after_cleanup(pg_service, scheduled_jobs):
    async with build_client(pg_service, FakeImageService()) as client:
        first = await client.post("/api/v2/images/generate/batch", json=batch_payload("job-3"))
        # 백그라운드 작업 실패 시 generate_images_in_background가 수행하는 정리 동작
        await pg_service.delete_image_job_by_id("job-3")
        second = await client.post("/api/v2/images/generate/batch", json=batch_payload("job-3"))

    assert first.status_code == 202
    assert second.status_code == 202
    assert scheduled_jobs == ["job-3", "job-3"]
