from __future__ import annotations

from unittest.mock import patch

from app.services.keyword_manage_service import load_keyword_manage_payload


def test_manage_payload_passes_through_effective_refresh_interval_hours():
    repository_payload = {
        "updated_at": "2026-07-13T10:00:00",
        "groups": [
            {
                "group_id": "group_1",
                "label": "测试组",
                "order": 1,
                "keywords": [
                    {
                        "keyword_id": "kw_1",
                        "keyword_text": "观察期测试词",
                        "refresh_frequency_days": 1,
                        "effective_refresh_interval_hours": 3,
                        "refresh_frequency_source": "auto",
                        "lifecycle_stage": "observing",
                    }
                ],
            }
        ],
    }
    with (
        patch(
            "app.services.keyword_manage_service._keyword_repo"
        ) as keyword_repo,
        patch(
            "app.services.keyword_manage_service._build_keyword_stats",
            return_value={},
        ),
    ):
        keyword_repo.return_value.load.return_value = repository_payload
        payload = load_keyword_manage_payload()

    keyword = payload["groups"][0]["keywords"][0]
    assert keyword["refresh_frequency_days"] == 1
    assert keyword["effective_refresh_interval_hours"] == 3
