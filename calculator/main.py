
import ast
import operator

import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# ZITADEL configuration
ISSUER = "https://kubetoole-il8h58.ch1.zitadel.cloud"
PROJECT_ID = "400874651212903737"
JWKS_URL = f"{ISSUER}/oauth/v2/keys"

jwks_client = PyJWKClient(JWKS_URL)
bearer = HTTPBearer()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mayques.com",
        "https://www.mayques.com"
    ],
    allow_methods=["POST"],
    allow_headers=["Content-Type", "Authorization"],
)

# Authentication
def verify_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer)
):
    token = credentials.credentials

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=PROJECT_ID,
            options={"require": ["exp", "iss", "sub", "aud"]}
        )

        return payload["sub"]

    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token"
        )

# Calculator
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
            calculate(node.right)
        )

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calculate(node.operand)

    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value

    raise ValueError("Unsupported expression")

class Calculation(BaseModel):
    expression: str

@app.post("/calculate")
def evaluate(
    data: Calculation,
    user_id: str = Depends(verify_user)
):
    tree = ast.parse(data.expression, mode="eval")

    return {
        "result": calculate(tree)
    }
