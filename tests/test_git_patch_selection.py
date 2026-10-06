from git_patch_selection import build_selected_patch, normalize_selected_line_numbers


def test_normalize_selected_line_numbers_filters_non_positive_and_deduplicates() -> None:
    old_lines, new_lines, error = normalize_selected_line_numbers([0, 2, 2, -1], [3, 3, 0])

    assert error is None
    assert old_lines == {2}
    assert new_lines == {3}


def test_normalize_selected_line_numbers_requires_any_positive_selection() -> None:
    old_lines, new_lines, error = normalize_selected_line_numbers([0, -1], [0, -2])

    assert old_lines == set()
    assert new_lines == set()
    assert error == "At least one positive old or new line number is required."


def test_build_selected_patch_returns_empty_when_no_matching_lines() -> None:
    diff_text = """diff --git a/example.txt b/example.txt
index 1111111..2222222 100644
--- a/example.txt
+++ b/example.txt
@@ -1,2 +1,2 @@
-old
+new
"""

    patch = build_selected_patch(diff_text, {99}, {99})

    assert patch == ""


def test_build_selected_patch_includes_only_selected_changed_lines() -> None:
    diff_text = """diff --git a/example.txt b/example.txt
index 1111111..2222222 100644
--- a/example.txt
+++ b/example.txt
@@ -1,6 +1,6 @@
 keep-1
-a
+a-new
 keep-2
-b
+b-new
 keep-3
"""

    patch = build_selected_patch(diff_text, {4}, {4})

    assert "-b" in patch
    assert "+b-new" in patch
    assert "-a" not in patch
    assert "+a-new" not in patch


def test_build_selected_patch_preserves_context_around_selected_lines() -> None:
    diff_text = """diff --git a/example.txt b/example.txt
index 1111111..2222222 100644
--- a/example.txt
+++ b/example.txt
@@ -1,9 +1,9 @@
 keep-1
-alpha
+alpha-new
 keep-2
-beta
+beta-new
 keep-3
-gamma
+gamma-new
 keep-4
"""

    patch = build_selected_patch(diff_text, {4}, {4})

    assert " keep-2" in patch
    assert " keep-3" in patch


def test_build_selected_patch_selects_additions_and_removals_from_large_hunk() -> None:
    diff_text = """diff --git a/example.txt b/example.txt
index 1111111..2222222 100644
--- a/example.txt
+++ b/example.txt
@@ -1,4 +1,14 @@
 keep
-old-1
-old-2
+new-1
+new-2
+new-3
+new-4
+new-5
+new-6
+new-7
+new-8
+new-9
+new-10
 tail
"""

    patch = build_selected_patch(diff_text, {2}, {3, 4})

    assert patch == """diff --git a/example.txt b/example.txt
index 1111111..2222222 100644
--- a/example.txt
+++ b/example.txt
@@ -1,2 +1,1 @@
 keep
-old-1
@@ -4,0 +3,2 @@
+new-2
+new-3
"""

