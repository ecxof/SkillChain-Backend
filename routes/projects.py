from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def projects_root():
    return {"message": "Projects route"}