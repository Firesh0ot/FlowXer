from fastapi import APIRouter

from flowxer.api.routes import router as v1_router

router = APIRouter()
router.include_router(v1_router)
