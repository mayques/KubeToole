
import ast
import operator
import os
from contextlib import asynccontextmanager

import jwt
import psycopg
from jwt import PyJWKClient
from psycopg.rows import dict_row

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# PostgreSQL
# ============================================================

def database_connection():
    return psycopg.connect(
        host="postgres-service",
        port=5432,
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        row_factory=dict_row,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the calculations table if it does not exist.
    with database_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calculations (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id TEXT NOT NULL,
                expression TEXT NOT NULL,
                result DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS calculations_user_id_idx
            ON calculations (user_id, created_at DESC)
        """)

    yield


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(lifespan=lifespan)


# ============================================================
# ZITADEL Authentication
# ============================================================

ISSUER = "https://kubetoole-il8h58.ch1.zitadel.cloud"
PROJECT_ID = "400874651212903737"
JWKS_URL = f"{ISSUER}/oauth/v2/keys"

jwks_client = PyJWKClient(JWKS_URL)
bearer = HTTPBearer()


def verify_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
) -> str:

    token = credentials.credentials

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=PROJECT_ID,
            options={
                "require": ["exp", "iss", "sub", "aud"]
            },
        )

        return payload["sub"]

    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token",
        )


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mayques.com",
        "https://www.mayques.com",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Content-Type",
        "Authorization",
    ],
)


# ============================================================
# Calculator
# ============================================================

operations = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def calculate(node):
    if isinstance(node, ast.Expression):
        return calculate(node.body)

    if isinstance(node, ast.BinOp) and type(node.op) in operations:
        return operations[type(node.op)](
            calculate(node.left),
            calculate(node.right),
        )

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calculate(node.operand)

    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value

    raise ValueError("Unsupported expression")


class Calculation(BaseModel):
    expression: str


# ============================================================
# Calculate and store result
# ============================================================

@app.post("/calculate")
def evaluate(
    data: Calculation,
    user_id: str = Depends(verify_user),
):
    try:
        tree = ast.parse(data.expression, mode="eval")
        result = calculate(tree)

    except (
        SyntaxError,
        ValueError,
        ZeroDivisionError,
        OverflowError,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid calculation",
        )

    with database_connection() as conn:
        conn.execute(
            """
            INSERT INTO calculations (
                user_id,
                expression,
                result
            )
            VALUES (%s, %s, %s)
            """,
            (user_id, data.expression, result),
        )

    return {"result": result}


# ============================================================
# Retrieve authenticated user's calculation history
# ============================================================

@app.get("/history")
def history(
    user_id: str = Depends(verify_user),
):
    with database_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                expression,
                result,
                created_at
            FROM calculations
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()

    return {"calculations": rows}
