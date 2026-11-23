from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from pydantic import BaseModel
import ast
import operator

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mayques.com",
        "https://www.mayques.com"
    ],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

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
        return operations[type(node.op)](calculate(node.left), calculate(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calculate(node.operand)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    raise ValueError('Unsupported expression')


class Calculation(BaseModel):
    expression: str


@app.post('/calculate')
def evaluate(data: Calculation):
    tree = ast.parse(data.expression, mode='eval')
    return {'result': calculate(tree)}
