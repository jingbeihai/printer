"""Validate per-class copy counts shared by presets and submitted tasks."""
import json
import re


def expand_special_copies(classes: int, default: int, rules) -> list[int]:
    if not isinstance(classes, int) or isinstance(classes, bool) or not 1 <= classes <= 10000:
        raise ValueError("班级数必须在 1 到 10000 之间")
    if not isinstance(default, int) or isinstance(default, bool) or not 1 <= default <= 10000:
        raise ValueError("默认每班份数必须在 1 到 10000 之间")
    if not isinstance(rules, list):
        raise ValueError("特殊班级设置格式无效")
    result = [default] * classes
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('classes'), str):
            raise ValueError("请填写特殊班级及份数")
        copies = rule.get('copies')
        if not isinstance(copies, int) or isinstance(copies, bool) or not 1 <= copies <= 10000:
            raise ValueError("特殊班级份数必须在 1 到 10000 之间")
        tokens = re.split(r'[,，、\s]+', rule['classes'].strip())
        if not tokens or any(not token for token in tokens):
            raise ValueError("特殊班级范围不能为空")
        for token in tokens:
            if not re.fullmatch(r'\d+(?:-\d+)?', token):
                raise ValueError(f"班级范围格式无效：{token}")
            bounds = [int(value) for value in token.split('-')]
            start, end = (bounds[0], bounds[-1])
            if start < 1 or end > classes or start > end:
                raise ValueError(f"班级范围超出 1 到 {classes} 班：{token}")
            for number in range(start, end + 1):
                if number in seen:
                    raise ValueError(f"第 {number} 班重复设置")
                seen.add(number)
                result[number - 1] = copies
    return result


def validate_class_copies(classes: int, default: int, values) -> list[int]:
    if values is None:
        return expand_special_copies(classes, default, [])
    if not isinstance(values, list) or len(values) != classes:
        raise ValueError("逐班份数数量与班级数不一致")
    if any(not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10000
           for value in values):
        raise ValueError("逐班份数必须是 1 到 10000 的整数")
    return values


def stored_class_copies(classes: int, default: int, encoded: str) -> list[int]:
    return validate_class_copies(classes, default, json.loads(encoded or 'null'))
