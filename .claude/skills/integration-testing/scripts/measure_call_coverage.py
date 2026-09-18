#!/usr/bin/env python3
"""통합시험 구조적 커버리지(함수 커버리지, Call 커버리지) 측정 스크립트.

ISO 26262-6 구조적 커버리지 지표(소프트웨어 아키텍처 수준)인 함수 커버리지와
Call 커버리지를 측정한다.

동작:
1. --src 아래 소스를 정적 분석(ast)해 함수 집합과 호출 간선(콜그래프)을 만든다.
   자기 모듈 내 직접 호출, self/cls 메서드 호출, import로 연결된 모듈 간 호출을
   해석한다. 동적 디스패치(타입 추론이 필요한 임의 객체의 메서드 호출)는 해석하지
   못하므로 --extra-edges로 보완할 수 있다.
2. --tests 아래 unittest 테스트를 discover로 찾아 실행하는 동안 sys.setprofile로
   실제 호출을 추적한다.
3. 정적 그래프 대비 실행된 함수/간선의 비율을 계산한다.

주의: 정적 콜그래프는 최선 노력(best-effort) 근사치다. 아키텍처 설계서(§6 인터페이스
명세)에 정의되어 있으나 이 스크립트가 해석하지 못하는 호출 관계가 있다면
--extra-edges JSON으로 추가하거나, 결과 보고서의 "정적 분석 한계" 절에 수동으로
기록한다.
"""

import argparse
import ast
import json
import sys
import unittest
from pathlib import Path


def relativeModuleName(path, srcRoot):
    rel = path.relative_to(srcRoot).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


class ImportCollector(ast.NodeVisitor):
    """모듈 별칭 -> 실제 모듈명을 수집한다(srcRoot 내부 모듈만)."""

    def __init__(self, knownModules):
        self.knownModules = knownModules
        self.aliasToModule = {}
        self.nameToModule = {}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in self.knownModules:
                self.aliasToModule[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module in self.knownModules:
            for alias in node.names:
                self.nameToModule[alias.asname or alias.name] = node.module
        self.generic_visit(node)


class FunctionCollector(ast.NodeVisitor):
    """함수/메서드 정의를 찾아 함수 집합과 (moduleName, 이름) -> qualifiedName을 만든다."""

    def __init__(self, moduleName):
        self.moduleName = moduleName
        self.functions = set()
        self.owners = {}
        self.classStack = []

    def visit_ClassDef(self, node):
        self.classStack.append(node.name)
        self.generic_visit(node)
        self.classStack.pop()

    def visit_FunctionDef(self, node):
        self._recordFunction(node)

    def visit_AsyncFunctionDef(self, node):
        self._recordFunction(node)

    def _recordFunction(self, node):
        ownerClass = self.classStack[-1] if self.classStack else None
        simpleName = f"{ownerClass}.{node.name}" if ownerClass else node.name
        qualifiedName = f"{self.moduleName}::{simpleName}"
        self.functions.add(qualifiedName)
        self.owners[simpleName] = qualifiedName
        if ownerClass is None:
            self.owners[node.name] = qualifiedName
        self.generic_visit(node)


class CallCollector(ast.NodeVisitor):
    """함수 본문의 호출을 찾아 (호출자, 호출대상) 간선을 만든다."""

    def __init__(self, moduleName, localOwners, globalOwners, aliasToModule, nameToModule):
        self.moduleName = moduleName
        self.localOwners = localOwners
        self.globalOwners = globalOwners
        self.aliasToModule = aliasToModule
        self.nameToModule = nameToModule
        self.classStack = []
        self.funcStack = []
        self.edges = set()

    def visit_ClassDef(self, node):
        self.classStack.append(node.name)
        self.generic_visit(node)
        self.classStack.pop()

    def visit_FunctionDef(self, node):
        self._enterFunction(node)

    def visit_AsyncFunctionDef(self, node):
        self._enterFunction(node)

    def _enterFunction(self, node):
        ownerClass = self.classStack[-1] if self.classStack else None
        simpleName = f"{ownerClass}.{node.name}" if ownerClass else node.name
        qualifiedName = f"{self.moduleName}::{simpleName}"
        self.funcStack.append(qualifiedName)
        self.generic_visit(node)
        self.funcStack.pop()

    def visit_Call(self, node):
        if self.funcStack:
            callee = self._resolveCallee(node)
            if callee:
                self.edges.add((self.funcStack[-1], callee))
        self.generic_visit(node)

    def _resolveCallee(self, node):
        ownerClass = self.classStack[-1] if self.classStack else None
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in self.localOwners:
                return self.localOwners[func.id]
            if func.id in self.nameToModule:
                targetModule = self.nameToModule[func.id]
                return self.globalOwners.get((targetModule, func.id))
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in ("self", "cls") and ownerClass:
                return self.localOwners.get(f"{ownerClass}.{func.attr}")
            if func.value.id in self.aliasToModule:
                targetModule = self.aliasToModule[func.value.id]
                return self.globalOwners.get((targetModule, func.attr))
        return None


def collectStaticGraph(srcRoot):
    files = [p for p in sorted(srcRoot.rglob("*.py")) if "__pycache__" not in p.parts]
    moduleTrees = {}
    for path in files:
        moduleName = relativeModuleName(path, srcRoot)
        try:
            moduleTrees[moduleName] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"경고: {path} 파싱 실패 - {exc}", file=sys.stderr)

    knownModules = set(moduleTrees)
    functions = set()
    localOwnersByModule = {}
    globalOwners = {}
    for moduleName, tree in moduleTrees.items():
        collector = FunctionCollector(moduleName)
        collector.visit(tree)
        functions |= collector.functions
        localOwnersByModule[moduleName] = collector.owners
        for simpleName, qualifiedName in collector.owners.items():
            globalOwners[(moduleName, simpleName)] = qualifiedName

    edges = set()
    for moduleName, tree in moduleTrees.items():
        importCollector = ImportCollector(knownModules)
        importCollector.visit(tree)
        callCollector = CallCollector(
            moduleName, localOwnersByModule[moduleName], globalOwners,
            importCollector.aliasToModule, importCollector.nameToModule,
        )
        callCollector.visit(tree)
        edges |= callCollector.edges

    return functions, edges


def runTracedTests(testsPath, srcRoot):
    executedFunctions = set()
    executedEdges = set()
    callStack = []

    def resolveFrameName(frame):
        filename = Path(frame.f_code.co_filename)
        try:
            filename.relative_to(srcRoot)
        except ValueError:
            return None
        moduleName = relativeModuleName(filename, srcRoot)
        qualname = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
        if ".<locals>." in qualname:
            qualname = qualname.split(".<locals>.")[-1]
        return f"{moduleName}::{qualname}"

    def profiler(frame, event, _arg):
        if event == "call":
            qualifiedName = resolveFrameName(frame)
            callStack.append(qualifiedName)
            if qualifiedName:
                executedFunctions.add(qualifiedName)
                caller = next((c for c in reversed(callStack[:-1]) if c), None)
                if caller:
                    executedEdges.add((caller, qualifiedName))
        elif event == "return":
            if callStack:
                callStack.pop()

    testsPath = Path(testsPath)
    loader = unittest.TestLoader()
    if testsPath.is_file():
        suite = loader.discover(str(testsPath.parent), pattern=testsPath.name)
    else:
        suite = loader.discover(str(testsPath))

    sys.setprofile(profiler)
    try:
        result = unittest.TextTestRunner(verbosity=0).run(suite)
    finally:
        sys.setprofile(None)

    return executedFunctions, executedEdges, result


def loadExtraEdges(path):
    if not path:
        return set()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(item["caller"], item["callee"]) for item in data}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="통합시험 함수/Call 커버리지 측정")
    parser.add_argument("--src", required=True, help="운영 코드 소스 디렉터리")
    parser.add_argument("--tests", required=True, help="unittest 테스트 디렉터리 또는 파일")
    parser.add_argument("--extra-edges", default=None,
                         help='정적 분석이 놓친 간선을 보완하는 JSON 파일: [{"caller": "...", "callee": "..."}]')
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = parser.parse_args()

    srcRoot = Path(args.src).resolve()
    staticFunctions, staticEdges = collectStaticGraph(srcRoot)
    staticEdges |= loadExtraEdges(args.extra_edges)

    executedFunctions, executedEdges, result = runTracedTests(args.tests, srcRoot)

    coveredFunctions = staticFunctions & executedFunctions
    coveredEdges = staticEdges & executedEdges
    missingFunctions = sorted(staticFunctions - executedFunctions)
    missingEdges = sorted(staticEdges - executedEdges)

    functionCoverage = (len(coveredFunctions) / len(staticFunctions)) if staticFunctions else 1.0
    callCoverage = (len(coveredEdges) / len(staticEdges)) if staticEdges else 1.0

    passed = functionCoverage >= 1.0 and callCoverage >= 1.0

    if args.json:
        output = {
            "functionCoverage": round(functionCoverage, 4),
            "callCoverage": round(callCoverage, 4),
            "totalFunctions": len(staticFunctions),
            "coveredFunctions": len(coveredFunctions),
            "missingFunctions": missingFunctions,
            "totalEdges": len(staticEdges),
            "coveredEdges": len(coveredEdges),
            "missingEdges": [f"{c} -> {e}" for c, e in missingEdges],
            "testsRun": result.testsRun,
            "testFailures": len(result.failures) + len(result.errors),
            "result": "PASS" if passed else "FAIL",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"함수 커버리지: {len(coveredFunctions)}/{len(staticFunctions)} ({functionCoverage:.1%})")
        if missingFunctions:
            print("  미실행 함수:")
            for name in missingFunctions:
                print(f"    - {name}")
        print(f"Call 커버리지: {len(coveredEdges)}/{len(staticEdges)} ({callCoverage:.1%})")
        if missingEdges:
            print("  미실행 호출 간선:")
            for caller, callee in missingEdges:
                print(f"    - {caller} -> {callee}")
        print(f"테스트 실행: {result.testsRun}건, 실패/에러: {len(result.failures) + len(result.errors)}건")
        print("\nRESULT: PASS" if passed else "\nRESULT: FAIL (100% 미달)")

    sys.exit(0 if passed and result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
