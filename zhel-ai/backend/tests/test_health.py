"""Тест health-эндпоинта, по которому compose проверяет живость контейнера."""

from httpx import AsyncClient


async def test_health_reports_status_and_environment(client: AsyncClient):
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "environment": "local", "version": "0.1.0"}
