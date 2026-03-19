import re

def parse_generic_layout(text):

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    rx_code = re.compile(r"[A-Z]{2,}\d{3,}")
    rx_money = re.compile(r"\d{1,3}(?:[\.,]\d{3})+")

    items = []

    i = 0
    n = len(lines)

    while i < n:

        line = lines[i]

        if not rx_code.search(line):
            i += 1
            continue

        code = rx_code.search(line).group(0)

        desc_parts = []

        i += 1

        while i < n and not rx_money.search(lines[i]):
            desc_parts.append(lines[i])
            i += 1

        qty = 1
        unit = 0
        total = 0

        if i < n and rx_money.search(lines[i]):
            unit = int(re.sub(r"[^\d]", "", rx_money.search(lines[i]).group(0)))
            i += 1

        if i < n and rx_money.search(lines[i]):
            total = int(re.sub(r"[^\d]", "", rx_money.search(lines[i]).group(0)))
            i += 1

        desc = " ".join(desc_parts).strip()

        items.append({
            "descripcion": f"{code} - {desc}",
            "cantidad": qty,
            "precio_unitario": unit,
            "total": total
        })

    return items
