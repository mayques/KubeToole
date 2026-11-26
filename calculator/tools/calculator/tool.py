import ast
import math
import operator

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def calculate(node):
    if isinstance(node, ast.Expression):
        return calculate(node.body)
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](calculate(node.left), calculate(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calculate(node.operand)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    raise ValueError("Unsupported expression")


def run(inputs, context):
    tree = ast.parse(inputs["expression"], mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 256:
        raise ValueError("Expression too complex")
    result = calculate(tree)
    if not math.isfinite(result):
        raise ValueError("Result must be finite")
    return {"result": result}
