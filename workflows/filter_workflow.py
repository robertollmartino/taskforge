import json
from datetime import timedelta
from typing import Any
from core.config import settings
from core.security import can_user_access_task


PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def _extract_field_value(task: dict[str, Any], field_name: str) -> Any:
    if field_name.startswith("custom."):
        cf_key = field_name.split("custom.", 1)[1]
        custom_vals = task.get("custom_values", {})
        if isinstance(custom_vals, dict):
            return custom_vals.get(cf_key)
        return None

    if field_name in task:
        return task[field_name]

    custom_vals = task.get("custom_values", {})
    if isinstance(custom_vals, dict) and field_name in custom_vals:
        return custom_vals.get(field_name)
    return None


def _to_number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace("R$", "").replace(" ", "").replace(",", ".")
        return float(cleaned)
    except Exception:
        return None


def evaluate_condition(
    task: dict[str, Any],
    condition: dict[str, Any],
    today_str: str,
    next_7_str: str,
) -> bool:
    field = str(condition.get("field") or "").strip()
    op = str(condition.get("op") or "eq").strip().lower()
    target = condition.get("value")

    if not field:
        return True

    actual = _extract_field_value(task, field)

    # Empty / Not empty checks
    if op == "is_empty":
        if actual is None or actual == "" or actual == [] or actual == {}:
            return True
        return False
    if op == "is_not_empty":
        if actual is None or actual == "" or actual == [] or actual == {}:
            return False
        return True

    # Date relative operators
    if op == "is_today":
        return bool(actual) and str(actual)[:10] == today_str
    if op == "is_overdue":
        if not actual:
            return False
        d_str = str(actual)[:10]
        if d_str < today_str:
            return True
        if d_str == today_str and task.get("due_time"):
            now_hm = settings.now().strftime("%H:%M")
            return str(task["due_time"]) < now_hm
        return False
    if op == "next_7_days":
        if not actual:
            return False
        d_str = str(actual)[:10]
        return today_str <= d_str <= next_7_str
    if op == "is_future":
        if not actual:
            return False
        d_str = str(actual)[:10]
        return d_str > today_str

    # Boolean checks
    if op in ("is_true", "true"):
        return bool(actual) is True or str(actual).lower() in ("true", "1", "sim", "yes")
    if op in ("is_false", "false"):
        return bool(actual) is False or str(actual).lower() in ("false", "0", "nao", "não", "no", "")

    # Membership operators (in, not_in)
    if op in ("in", "not_in"):
        target_list = (
            target
            if isinstance(target, list)
            else [x.strip() for x in str(target or "").split(",") if x.strip()]
        )
        if isinstance(actual, list):
            matched = any(str(item) in [str(t) for t in target_list] for item in actual)
        else:
            matched = str(actual) in [str(t) for t in target_list]
        return matched if op == "in" else not matched

    # Text / List contains
    if op in ("contains", "not_contains"):
        if actual is None:
            return op == "not_contains"
        t_str = str(target or "").lower()
        if isinstance(actual, list):
            matched = any(t_str in str(item).lower() for item in actual)
        else:
            matched = t_str in str(actual).lower()
        return matched if op == "contains" else not matched

    if op == "starts_with":
        return actual is not None and str(actual).lower().startswith(str(target or "").lower())
    if op == "ends_with":
        return actual is not None and str(actual).lower().endswith(str(target or "").lower())

    # Numeric or Date range comparisons
    if op in ("gt", "gte", "lt", "lte"):
        num_a = _to_number(actual)
        num_b = _to_number(target)
        if num_a is not None and num_b is not None:
            if op == "gt":
                return num_a > num_b
            if op == "gte":
                return num_a >= num_b
            if op == "lt":
                return num_a < num_b
            if op == "lte":
                return num_a <= num_b
        # Fallback to string / ISO date comparison
        if actual is None or target is None:
            return False
        sa, sb = str(actual), str(target)
        if op == "gt":
            return sa > sb
        if op == "gte":
            return sa >= sb
        if op == "lt":
            return sa < sb
        if op == "lte":
            return sa <= sb

    # Equality (eq / neq)
    if op in ("eq", "neq"):
        if isinstance(actual, list):
            is_eq = str(target) in [str(x) for x in actual]
        elif isinstance(actual, bool) or isinstance(target, bool):
            is_eq = str(actual).lower() == str(target).lower()
        else:
            num_a = _to_number(actual)
            num_b = _to_number(target)
            if num_a is not None and num_b is not None:
                is_eq = num_a == num_b
            else:
                is_eq = str(actual or "").strip().lower() == str(target or "").strip().lower()
        return is_eq if op == "eq" else not is_eq

    return True


def evaluate_filter_group(
    task: dict[str, Any],
    group: dict[str, Any],
    today_str: str,
    next_7_str: str,
) -> bool:
    if not group or not isinstance(group, dict):
        return True

    logic = str(group.get("logic") or "AND").upper()
    conditions = group.get("conditions") or []
    subgroups = group.get("groups") or []

    results: list[bool] = []
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("field"):
            results.append(evaluate_condition(task, cond, today_str, next_7_str))

    for sub in subgroups:
        if isinstance(sub, dict):
            results.append(evaluate_filter_group(task, sub, today_str, next_7_str))

    if not results:
        return True

    if logic == "OR":
        return any(results)
    return all(results)


def matches_global_search(task: dict[str, Any], search_query: str) -> bool:
    q = search_query.strip().lower()
    if not q:
        return True

    haystack_parts = [
        f"#{task.get('code', '')}",
        str(task.get("title") or ""),
        str(task.get("description") or ""),
        str(task.get("notes") or ""),
        str(task.get("priority") or ""),
        str(task.get("status_name") or task.get("status_slug") or ""),
        str(task.get("project_name") or ""),
        str(task.get("assignee_name") or ""),
        " ".join(str(t) for t in (task.get("tags") or [])),
    ]
    custom_vals = task.get("custom_values") or {}
    if isinstance(custom_vals, dict):
        for k, v in custom_vals.items():
            haystack_parts.append(f"{k}:{v}")

    for st in task.get("subtasks") or []:
        if isinstance(st, dict):
            haystack_parts.append(str(st.get("title") or ""))

    full_text = " ".join(haystack_parts).lower()
    terms = [t for t in q.split() if t]
    return all(term in full_text for term in terms)


def sort_tasks(tasks: list[dict[str, Any]], sorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not sorts:
        return tasks

    result = list(tasks)
    # Apply sorts in reverse order so primary sort is applied last (Python Timsort is stable)
    for sort_spec in reversed(sorts):
        field = str(sort_spec.get("field") or "created_at")
        direction = str(sort_spec.get("dir") or "asc").lower()
        reverse = direction == "desc"

        def sort_key(t: dict[str, Any], f: str = field, rev: bool = reverse) -> tuple[int, Any]:
            val = _extract_field_value(t, f)
            if val is None or val == "":
                # Always push empty/null values to the bottom regardless of asc/desc
                return (0 if rev else 1, "")
            if f == "priority":
                return (0, PRIORITY_RANK.get(str(val).upper(), 99))
            num = _to_number(val)
            if num is not None:
                return (0, num)
            return (0, str(val).lower())

        result.sort(key=sort_key, reverse=reverse)
    return result


def filter_and_paginate_tasks(
    all_tasks: list[dict[str, Any]],
    user: dict[str, Any],
    filter_tree: dict[str, Any] | None = None,
    search_query: str = "",
    project_ids: list[str] | None = None,
    sorts: list[dict[str, Any]] | None = None,
    group_by: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    now = settings.now()
    today_str = now.strftime("%Y-%m-%d")
    next_7_str = (now + timedelta(days=7)).strftime("%Y-%m-%d")

    filtered: list[dict[str, Any]] = []
    for t in all_tasks:
        # 1. Enforce backend RBAC permission per task & project
        if not can_user_access_task(user, t):
            continue
        # 2. Multi-project filter if provided
        if project_ids:
            if t.get("project_id") not in project_ids:
                continue
        # 3. Global search
        if search_query and not matches_global_search(t, search_query):
            continue
        # 4. Nested AND/OR filter tree
        if filter_tree and not evaluate_filter_group(t, filter_tree, today_str, next_7_str):
            continue
        filtered.append(t)

    sorted_list = sort_tasks(
        filtered,
        sorts or [{"field": "position", "dir": "asc"}, {"field": "created_at", "dir": "desc"}],
    )

    total = len(sorted_list)
    page = max(1, page)
    page_size = max(1, min(1000, page_size))
    start_idx = (page - 1) * page_size
    paginated = sorted_list[start_idx : start_idx + page_size]

    grouped: dict[str, list[dict[str, Any]]] = {}
    if group_by:
        for item in paginated:
            g_val = _extract_field_value(item, group_by)
            if isinstance(g_val, list):
                g_key = ", ".join(str(x) for x in g_val) if g_val else "Sem valor"
            else:
                g_key = str(g_val) if g_val not in (None, "") else "Sem valor"
            grouped.setdefault(g_key, []).append(item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": paginated,
        "groups": grouped if group_by else None,
    }
