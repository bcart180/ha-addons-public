"""0.9.4: the Climate page is not a CSS size container."""
import os

INDEX = os.path.join(os.path.dirname(__file__), "..", "rootfs", "app", "static", "index.html")


def test_index_has_no_size_container():
    html = open(INDEX, encoding="utf-8").read()
    assert "container-type" not in html
    assert "container:" not in html
    assert "@container" not in html
    start = html.index("@media (min-width:932px){")
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = html[start:end]
    assert ".grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}" in block
    assert ".wide{grid-column:1 / -1}" in block
    assert ".metrics{grid-template-columns:repeat(4,minmax(0,1fr))}" in block
    assert "scrollIntoView" not in html
    assert "scrollTo(0,0)" not in html and "scrollTo(0, 0)" not in html
    assert ".focus(" not in html
