import os
import csv
import io
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
from starlette.responses import Response
import databases
import sqlalchemy

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./waitlist.db")
ADMIN_KEY = os.getenv("ADMIN_KEY", "osogbo-admin-2024")

database = databases.Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

waitlist_table = sqlalchemy.Table(
    "waitlist",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True, autoincrement=True),
    sqlalchemy.Column("email", sqlalchemy.String(255), unique=True, nullable=False),
    sqlalchemy.Column("whatsapp", sqlalchemy.String(50), nullable=True),
    sqlalchemy.Column("role", sqlalchemy.String(50), nullable=True),
    sqlalchemy.Column("interests", sqlalchemy.Text, nullable=True),
    sqlalchemy.Column("source", sqlalchemy.String(50), nullable=True),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, nullable=False),
)

engine = sqlalchemy.create_engine(DATABASE_URL.replace("sqlite:///", "sqlite:///"))
metadata.create_all(engine)

app = FastAPI(title="Osogbo Live API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await database.connect()


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


class WaitlistEntry(BaseModel):
    email: EmailStr
    whatsapp: Optional[str] = None
    source: Optional[str] = "organic"


class SegmentUpdate(BaseModel):
    email: EmailStr
    role: Optional[str] = None
    interests: Optional[list[str]] = None


@app.post("/api/waitlist")
async def join_waitlist(entry: WaitlistEntry):
    existing = await database.fetch_one(
        waitlist_table.select().where(waitlist_table.c.email == entry.email.lower().strip())
    )
    if existing:
        raise HTTPException(status_code=409, detail="Email already on the waitlist.")

    await database.execute(
        waitlist_table.insert().values(
            email=entry.email.lower().strip(),
            whatsapp=entry.whatsapp,
            source=entry.source,
            created_at=datetime.now(timezone.utc),
        )
    )
    return {"status": "ok", "message": "You're on the list!"}


@app.post("/api/segment")
async def update_segment(data: SegmentUpdate):
    interests_str = ",".join(data.interests) if data.interests else None
    await database.execute(
        waitlist_table.update()
        .where(waitlist_table.c.email == data.email.lower().strip())
        .values(role=data.role, interests=interests_str)
    )
    return {"status": "ok"}


@app.get("/api/count")
async def get_count():
    result = await database.fetch_one(sqlalchemy.select(sqlalchemy.func.count()).select_from(waitlist_table))
    return {"count": result[0] if result else 0}


@app.get("/api/admin/export")
async def admin_export(key: str, format: str = "csv"):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    rows = await database.fetch_all(waitlist_table.select().order_by(waitlist_table.c.created_at.desc()))

    if format == "json":
        return [
            {
                "id": r["id"],
                "email": r["email"],
                "whatsapp": r["whatsapp"],
                "role": r["role"],
                "interests": r["interests"],
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "email", "whatsapp", "role", "interests", "source", "created_at"])
    for r in rows:
        writer.writerow([
            r["id"], r["email"], r["whatsapp"], r["role"],
            r["interests"], r["source"],
            r["created_at"].isoformat() if r["created_at"] else "",
        ])

    from starlette.responses import Response
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=osogbo_waitlist.csv"},
    )


@app.get("/api/admin/stats")
async def admin_stats(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    total = await database.fetch_one(sqlalchemy.select(sqlalchemy.func.count()).select_from(waitlist_table))

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = await database.fetch_one(
        sqlalchemy.select(sqlalchemy.func.count())
        .select_from(waitlist_table)
        .where(waitlist_table.c.created_at >= today_start)
    )

    roles_result = await database.fetch_all(
        sqlalchemy.select(waitlist_table.c.role, sqlalchemy.func.count())
        .group_by(waitlist_table.c.role)
    )

    return {
        "total_signups": total[0] if total else 0,
        "today_signups": today[0] if today else 0,
        "roles_breakdown": {r["role"] or "unset": r[1] for r in roles_result},
    }


class AdminLogin(BaseModel):
    key: str


class SubmissionUpdate(BaseModel):
    role: Optional[str] = None
    interests: Optional[str] = None


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    if body.key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")
    return {"status": "ok", "token": ADMIN_KEY}


@app.get("/api/admin/submissions")
async def admin_submissions(
    key: str,
    search: Optional[str] = None,
    role: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    query = waitlist_table.select()
    count_query = sqlalchemy.select(sqlalchemy.func.count()).select_from(waitlist_table)

    if search:
        like = f"%{search.lower()}%"
        query = query.where(waitlist_table.c.email.like(like))
        count_query = count_query.where(waitlist_table.c.email.like(like))

    if role:
        query = query.where(waitlist_table.c.role == role)
        count_query = count_query.where(waitlist_table.c.role == role)

    total_result = await database.fetch_one(count_query)
    total = total_result[0] if total_result else 0

    offset = (page - 1) * per_page
    query = query.order_by(waitlist_table.c.created_at.desc()).offset(offset).limit(per_page)
    rows = await database.fetch_all(query)

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "data": [
            {
                "id": r["id"],
                "email": r["email"],
                "whatsapp": r["whatsapp"],
                "role": r["role"],
                "interests": r["interests"],
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@app.put("/api/admin/submissions/{submission_id}")
async def update_submission(submission_id: int, body: SubmissionUpdate, key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    existing = await database.fetch_one(
        waitlist_table.select().where(waitlist_table.c.id == submission_id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Submission not found.")

    values = {}
    if body.role is not None:
        values["role"] = body.role
    if body.interests is not None:
        values["interests"] = body.interests

    if values:
        await database.execute(
            waitlist_table.update().where(waitlist_table.c.id == submission_id).values(**values)
        )

    return {"status": "ok"}


@app.delete("/api/admin/submissions/{submission_id}")
async def delete_submission(submission_id: int, key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key.")

    existing = await database.fetch_one(
        waitlist_table.select().where(waitlist_table.c.id == submission_id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Submission not found.")

    await database.execute(waitlist_table.delete().where(waitlist_table.c.id == submission_id))
    return {"status": "ok"}
