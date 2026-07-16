from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.security import get_current_user, get_current_admin_user
from ...database import get_db

router = APIRouter()


def _unique_name(db: Session, base_name: str, parent_id: Optional[str], exclude_id: Optional[str] = None) -> str:
    """Return base_name, or base_name (N) if a sibling with that name already exists."""
    q = db.query(models.Folder.name).filter(
        models.Folder.parent_id == parent_id,
    )
    if exclude_id:
        q = q.filter(models.Folder.id != exclude_id)
    existing = {row[0] for row in q.all()}
    if base_name not in existing:
        return base_name
    n = 1
    while f"{base_name} ({n})" in existing:
        n += 1
    return f"{base_name} ({n})"


def _get_folder_or_404(db: Session, folder_id: str) -> models.Folder:
    folder = db.query(models.Folder).filter(models.Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="資料夾不存在")
    return folder


def _build_doc_counts(db: Session) -> dict:
    """Return {folder_id: doc_count} for all folders."""
    rows = db.query(models.Document.folder_id).filter(
        models.Document.is_archived == False,  # noqa: E712
        models.Document.folder_id != None,  # noqa: E711
    ).all()
    counts: dict = {}
    for (fid,) in rows:
        counts[fid] = counts.get(fid, 0) + 1
    return counts


@router.get("", response_model=List[schemas.FolderRead])
def list_folders(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return all folders (flat list). Frontend builds the tree."""
    _ = current_user
    folders = db.query(models.Folder).order_by(
        models.Folder.parent_id.asc().nullsfirst(),
        models.Folder.order_index.asc(),
        models.Folder.name.asc(),
    ).all()
    counts = _build_doc_counts(db)
    result = []
    for f in folders:
        read = schemas.FolderRead.model_validate(f)
        read.doc_count = counts.get(f.id, 0)
        result.append(read)
    return result


@router.post("", response_model=schemas.FolderRead, status_code=status.HTTP_201_CREATED)
def create_folder(
    payload: schemas.FolderCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：資料夾為全域共用，寫入須 admin
):
    _ = current_user
    if payload.parent_id:
        _get_folder_or_404(db, payload.parent_id)

    pid = payload.parent_id or None
    name = _unique_name(db, payload.name.strip(), pid)
    folder = models.Folder(
        name=name,
        parent_id=pid,
        order_index=payload.order_index,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    read = schemas.FolderRead.model_validate(folder)
    read.doc_count = 0
    return read


@router.put("/{folder_id}", response_model=schemas.FolderRead)
def update_folder(
    folder_id: str,
    payload: schemas.FolderUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：資料夾為全域共用，寫入須 admin
):
    _ = current_user
    folder = _get_folder_or_404(db, folder_id)

    if payload.name is not None:
        folder.name = _unique_name(db, payload.name.strip(), folder.parent_id, exclude_id=folder_id)
    if payload.order_index is not None:
        folder.order_index = payload.order_index
    if payload.parent_id is not None:
        if payload.parent_id == "__root__":
            folder.parent_id = None
        else:
            # Prevent circular reference
            if payload.parent_id == folder_id:
                raise HTTPException(status_code=400, detail="資料夾不能以自己為父層")
            _get_folder_or_404(db, payload.parent_id)
            folder.parent_id = payload.parent_id

    db.commit()
    db.refresh(folder)
    counts = _build_doc_counts(db)
    read = schemas.FolderRead.model_validate(folder)
    read.doc_count = counts.get(folder.id, 0)
    return read


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：刪資料夾會影響全體，須 admin
):
    _ = current_user
    folder = _get_folder_or_404(db, folder_id)
    parent_id = folder.parent_id

    # Move child folders up to this folder's parent
    db.query(models.Folder).filter(models.Folder.parent_id == folder_id).update(
        {"parent_id": parent_id}
    )
    # Unassign documents from this folder
    db.query(models.Document).filter(models.Document.folder_id == folder_id).update(
        {"folder_id": None}
    )

    db.delete(folder)
    db.commit()
