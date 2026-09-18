#!/usr/bin/env python3
"""TDD 스킬 품질 게이트 검사 스크립트.

CLAUDE.md "구현 지침"의 품질 지표(순환복잡도/함수길이/중복코드/주석비율/네이밍)를
radon, pylint와 함께 함수 단위로 검사한다. 대상 경로 아래 *.py를 재귀적으로 수집하되
기본적으로 테스트 파일(test_*.py, *_test.py)은 제외한다.
"""

import argparse
import ast
import io
import json
import subprocess
import sys
import tokenize
from pathlib import Path

COMPLEXITY_THRESHOLD = 10
PURE_LINE_THRESHOLD = 50
COMMENT_RATIO_THRESHOLD = 0.20


def collectTargetFiles(target, includeTests):
    targetPath = Path(target)
    candidates = [targetPath] if targetPath.is_file() else sorted(targetPath.rglob("*.py"))
    result = []
    for path in candidates:
        if "__pycache__" in path.parts:
            continue
        name = path.name
        if not includeTests and (name.startswith("test_") or name.endswith("_test.py")):
            continue
        result.append(path)
    return result


def classifyLines(source):
    """각 물리 라인 번호(1-indexed)를 'code'/'comment'/'blank'로 분류한다."""
    lineHasCode = {}
    lineHasComment = {}
    skipTypes = (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                 tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            tokType, _, start, end, _ = tok
            if tokType == tokenize.COMMENT:
                lineHasComment[start[0]] = True
            elif tokType in skipTypes:
                continue
            else:
                for lineNum in range(start[0], end[0] + 1):
                    lineHasCode[lineNum] = True
    except tokenize.TokenError:
        pass
    totalLines = source.count("\n") + 1
    classification = {}
    for lineNum in range(1, totalLines + 1):
        if lineHasCode.get(lineNum):
            classification[lineNum] = "code"
        elif lineHasComment.get(lineNum):
            classification[lineNum] = "comment"
        else:
            classification[lineNum] = "blank"
    return classification


def findDoxygenCommentLines(sourceLines, defLineNo):
    """def 줄(1-indexed) 바로 위부터 연속된 '#'로 시작하는 줄 수를 센다."""
    count = 0
    idx = defLineNo - 2
    while idx >= 0 and sourceLines[idx].strip().startswith("#"):
        count += 1
        idx -= 1
    return count


def collectFunctions(source):
    """ast로 함수/메서드 정의를 찾아 (qualifiedName, lineno, endLineno) 목록을 만든다."""
    tree = ast.parse(source)
    functions = []

    def visit(node, ownerClass=None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualifiedName = f"{ownerClass}.{child.name}" if ownerClass else child.name
                endLineNo = getattr(child, "end_lineno", child.lineno)
                functions.append({"name": qualifiedName, "lineno": child.lineno, "endLineno": endLineNo})
                visit(child, ownerClass)
            elif isinstance(child, ast.ClassDef):
                visit(child, child.name)
            else:
                visit(child, ownerClass)

    visit(tree)
    return functions


def runRadon(path):
    """radon cc -j로 함수별 순환복잡도를 얻는다. {lineno: complexity} 반환."""
    try:
        proc = subprocess.run(["radon", "cc", "-j", str(path)], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("radon 실행 실패 - 'pip install radon'으로 설치되어 있는지 확인하세요.") from exc
    data = json.loads(proc.stdout or "{}")
    blocks = data.get(str(path), [])
    complexityByLine = {}
    for block in blocks:
        complexityByLine[block["lineno"]] = block["complexity"]
        for closure in block.get("closures", []):
            complexityByLine[closure["lineno"]] = closure["complexity"]
    return complexityByLine


def runPylint(paths, pylintrc):
    """pylint로 중복 코드(R0801)/네이밍(C0103) 위반을 수집한다."""
    if not paths:
        return [], []
    cmd = ["pylint", f"--rcfile={pylintrc}", "--disable=all",
           "--enable=duplicate-code,invalid-name", "--output-format=json"]
    cmd.extend(str(p) for p in paths)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError("pylint 실행 실패 - 'pip install pylint'로 설치되어 있는지 확인하세요.") from exc
    try:
        messages = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        messages = []
    duplicateMsgs = [m for m in messages if m.get("symbol") == "duplicate-code"]
    namingMsgs = [m for m in messages if m.get("symbol") == "invalid-name"]
    return duplicateMsgs, namingMsgs


def findEnclosingFunction(functions, lineNo):
    for func in functions:
        if func["lineno"] <= lineNo <= func["endLineno"]:
            return func["name"]
    return None


def checkFile(path, allDuplicateMsgs, allNamingMsgs):
    source = path.read_text(encoding="utf-8")
    sourceLines = source.splitlines()
    functions = collectFunctions(source)
    lineClassification = classifyLines(source)
    complexityByLine = runRadon(path)

    fileDuplicates = [m for m in allDuplicateMsgs if str(Path(m["path"])) in (str(path), path.name)]
    fileNaming = [m for m in allNamingMsgs if str(Path(m["path"])) in (str(path), path.name)]

    rows = []
    for func in functions:
        lineno, endLineNo, name = func["lineno"], func["endLineno"], func["name"]
        pureCodeLines = sum(1 for ln in range(lineno, endLineNo + 1) if lineClassification.get(ln) == "code")
        bodyCommentLines = sum(1 for ln in range(lineno, endLineNo + 1) if lineClassification.get(ln) == "comment")
        commentLines = bodyCommentLines + findDoxygenCommentLines(sourceLines, lineno)
        denominator = commentLines + pureCodeLines
        commentRatio = (commentLines / denominator) if denominator else 0.0
        complexity = complexityByLine.get(lineno)
        namingViolations = [m["message"] for m in fileNaming if findEnclosingFunction(functions, m["line"]) == name]

        rows.append({
            "file": str(path), "function": name, "lineno": lineno,
            "complexity": complexity,
            "complexityStatus": "PASS" if complexity is not None and complexity <= COMPLEXITY_THRESHOLD else "FAIL",
            "pureCodeLines": pureCodeLines,
            "pureLinesStatus": "PASS" if pureCodeLines <= PURE_LINE_THRESHOLD else "FAIL",
            "commentRatio": round(commentRatio, 3),
            "commentRatioStatus": "PASS" if commentRatio >= COMMENT_RATIO_THRESHOLD else "FAIL",
            "namingViolations": namingViolations,
            "namingStatus": "FAIL" if namingViolations else "PASS",
        })

    duplicateViolations = [m["message"] for m in fileDuplicates]
    return rows, duplicateViolations


def countViolations(allRows, fileDuplicateViolations):
    count = 0
    for row in allRows:
        for statusKey in ("complexityStatus", "pureLinesStatus", "commentRatioStatus"):
            if row[statusKey] == "FAIL":
                count += 1
        if row["namingViolations"]:
            count += len(row["namingViolations"])
    for violations in fileDuplicateViolations.values():
        count += len(violations)
    return count


def printReport(files, allRows, fileDuplicateViolations):
    for path in files:
        pathRows = [r for r in allRows if r["file"] == str(path)]
        if not pathRows and str(path) not in fileDuplicateViolations:
            continue
        print(f"\n{path}")
        for row in pathRows:
            print(f"  {row['function']} (line {row['lineno']})")
            print(f"    복잡도: {row['complexity']} <= {COMPLEXITY_THRESHOLD} -> {row['complexityStatus']}")
            print(f"    순수 코드 라인: {row['pureCodeLines']} <= {PURE_LINE_THRESHOLD} -> {row['pureLinesStatus']}")
            print(f"    주석 비율: {row['commentRatio']:.0%} >= {COMMENT_RATIO_THRESHOLD:.0%} -> {row['commentRatioStatus']}")
            suffix = f" ({'; '.join(row['namingViolations'])})" if row["namingViolations"] else ""
            print(f"    네이밍: {row['namingStatus']}{suffix}")
        if str(path) in fileDuplicateViolations:
            print("  중복 코드 위반:")
            for msg in fileDuplicateViolations[str(path)]:
                print(f"    - {msg}")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="TDD 품질 게이트 검사")
    parser.add_argument("target", help="검사할 파일 또는 디렉터리")
    parser.add_argument("--pylintrc", default=None, help=".pylintrc 경로")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    parser.add_argument("--include-tests", action="store_true", help="test_*.py/*_test.py도 검사 대상에 포함")
    args = parser.parse_args()

    pylintrc = args.pylintrc or str(Path(__file__).resolve().parent.parent / "assets" / ".pylintrc")
    files = collectTargetFiles(args.target, args.include_tests)
    if not files:
        print("검사 대상 .py 파일이 없습니다.", file=sys.stderr)
        sys.exit(1)

    try:
        allDuplicateMsgs, allNamingMsgs = runPylint(files, pylintrc)
        allRows = []
        fileDuplicateViolations = {}
        for path in files:
            rows, duplicateViolations = checkFile(path, allDuplicateMsgs, allNamingMsgs)
            allRows.extend(rows)
            if duplicateViolations:
                fileDuplicateViolations[str(path)] = duplicateViolations
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    violationCount = countViolations(allRows, fileDuplicateViolations)

    if args.json:
        output = {
            "rows": allRows,
            "duplicateViolations": fileDuplicateViolations,
            "result": "PASS" if violationCount == 0 else "FAIL",
            "violations": violationCount,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        printReport(files, allRows, fileDuplicateViolations)
        if violationCount == 0:
            print("\nRESULT: PASS")
        else:
            print(f"\nRESULT: FAIL ({violationCount} violations)")

    sys.exit(0 if violationCount == 0 else 1)


if __name__ == "__main__":
    main()
