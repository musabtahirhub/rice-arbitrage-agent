"""
Central API router aggregation.
"""

from fastapi import APIRouter
from app.api.campaigns import router as campaigns_router
from app.api.deals import router as deals_router
from app.api.simulation import router as simulation_router
from app.api.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api")

api_router.include_router(campaigns_router)
api_router.include_router(simulation_router)
api_router.include_router(webhooks_router)
api_router.include_router(deals_router)

__all__ = ["api_router"]
