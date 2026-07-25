"""Turn a graduated winner into a Meta ad.

Deliberately uses the explicit campaign -> ad set -> creative -> ad chain
rather than the one-shot boost endpoint, which is known not to work on this
ad account.

Everything is created PAUSED. Note that entities created through the API land
in Ads Manager as *unpublished drafts*: they sit behind "Review and Publish"
and are destroyed if the drafts are discarded. Nothing here starts spending on
its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .config import Config
from .http import ApiError, new_session, request

log = logging.getLogger(__name__)

# Below roughly 100 TRY/day an ad set never reaches the 50-results-in-7-days
# learning threshold, so it burns budget without ever optimising.
MIN_DAILY_BUDGET_TRY = 100


class AdsError(RuntimeError):
    pass


@dataclass
class AdChain:
    campaign_id: str = ""
    adset_id: str = ""
    creative_id: str = ""
    ad_id: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "adset_id": self.adset_id,
            "creative_id": self.creative_id,
            "ad_id": self.ad_id,
            "warnings": self.warnings,
        }


def default_targeting() -> dict:
    """Women 25-44 in Turkey.

    `advantage_audience: 0` is required to make the age bounds a hard limit;
    left on, Meta treats them as a suggestion and spends outside the range.
    """
    return {
        "geo_locations": {"countries": ["TR"]},
        "genders": [2],
        "age_min": 25,
        "age_max": 44,
        "targeting_automation": {"advantage_audience": 0},
    }


class MetaAds:
    def __init__(self, config: Config) -> None:
        if not config.ads_requirements_met():
            raise AdsError(
                "Ad creation needs META_AD_ACCOUNT_ID and FACEBOOK_PAGE_ID to be set."
            )
        self.config = config
        self.session = new_session()
        self.base = config.graph_base
        self.account = f"act_{config.ad_account_id}"

    def _post(self, edge: str, payload: dict, *, label: str) -> dict:
        body = {k: v for k, v in payload.items() if v not in (None, "", [])}
        body["access_token"] = self.config.ig_access_token
        response = request(
            self.session, "POST", f"{self.base}/{edge}", label=label, data=body
        )
        return response.json()

    def create_ad_from_ig_post(
        self,
        *,
        ig_media_id: str,
        name_prefix: str,
        daily_budget_try: int | None = None,
        targeting: dict | None = None,
        objective: str = "OUTCOME_ENGAGEMENT",
        optimization_goal: str = "REPLIES",
        destination_type: str = "WHATSAPP",
        billing_event: str = "IMPRESSIONS",
    ) -> AdChain:
        chain = AdChain()
        budget = daily_budget_try or self.config.ad_daily_budget_try

        if budget < MIN_DAILY_BUDGET_TRY:
            chain.warnings.append(
                f"Daily budget raised from {budget} to {MIN_DAILY_BUDGET_TRY} TRY: "
                "anything lower cannot clear the learning threshold."
            )
            budget = MIN_DAILY_BUDGET_TRY

        # 1. Campaign
        campaign = self._post(
            f"{self.account}/campaigns",
            {
                "name": f"{name_prefix} | kazanan reel",
                "objective": objective,
                "status": "PAUSED",
                "special_ad_categories": json.dumps([]),
            },
            label="ads create campaign",
        )
        chain.campaign_id = campaign["id"]
        log.info("Campaign %s created (PAUSED)", chain.campaign_id)

        # 2. Ad set
        adset_payload = {
            "name": f"{name_prefix} | ad set",
            "campaign_id": chain.campaign_id,
            "daily_budget": str(budget * 100),  # kurus
            "billing_event": billing_event,
            "optimization_goal": optimization_goal,
            "destination_type": destination_type,
            "targeting": json.dumps(targeting or default_targeting()),
            "promoted_object": json.dumps({"page_id": self.config.facebook_page_id}),
            "status": "PAUSED",
        }
        try:
            adset = self._post(f"{self.account}/adsets", adset_payload, label="ads create ad set")
        except ApiError as exc:
            # Messaging destinations are fussy; fall back to a plain engagement
            # ad set rather than leaving an orphaned campaign behind.
            log.warning("Ad set with destination %s failed: %s", destination_type, exc)
            chain.warnings.append(
                f"destination_type={destination_type} rejected, fell back to no destination. "
                "Check the ad set's messaging setup before publishing."
            )
            adset_payload.pop("destination_type")
            adset_payload.pop("promoted_object")
            adset_payload["optimization_goal"] = "POST_ENGAGEMENT"
            adset = self._post(f"{self.account}/adsets", adset_payload, label="ads create ad set (fallback)")
        chain.adset_id = adset["id"]
        log.info("Ad set %s created (PAUSED)", chain.adset_id)

        # 3. Creative from the existing IG post, preserving its social proof.
        creative = self._post(
            f"{self.account}/adcreatives",
            {
                "name": f"{name_prefix} | kreatif",
                "object_id": self.config.facebook_page_id,
                "instagram_user_id": self.config.ig_user_id,
                "source_instagram_media_id": ig_media_id,
            },
            label="ads create creative",
        )
        chain.creative_id = creative["id"]
        log.info("Creative %s created from IG media %s", chain.creative_id, ig_media_id)

        # 4. Ad
        ad = self._post(
            f"{self.account}/ads",
            {
                "name": f"{name_prefix} | reklam",
                "adset_id": chain.adset_id,
                "creative": json.dumps({"creative_id": chain.creative_id}),
                "status": "PAUSED",
            },
            label="ads create ad",
        )
        chain.ad_id = ad["id"]
        log.info("Ad %s created (PAUSED)", chain.ad_id)

        chain.warnings.append(
            "Created as an unpublished draft. Open Ads Manager -> 'Review and Publish' "
            "to confirm it, otherwise discarding drafts deletes it."
        )
        return chain
