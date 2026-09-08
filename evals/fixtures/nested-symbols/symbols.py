import ast

def symbol_names(source):
    tree = ast.parse(source)
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [node.name for node in tree.body if isinstance(node, kinds)]
