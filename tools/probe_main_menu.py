"""
أداة تشخيص: اطبع أزرار القائمة الرئيسية في MONEYADO (النص + الصنف + الموضع).

الاستخدام (وMONEYADO مفتوح على القائمة الرئيسية):
    .venv/Scripts/python.exe tools/probe_main_menu.py

الهدف: معرفة النص الفعلي لزرّي «بيع عملة»/«شراء عملة» وصنفهما، لضبط
config["main_menu"] في moneyado_fields.json إن اختلف عن الافتراضي (§11.3).
"""
from __future__ import annotations

from pywinauto import Application

PROCESS = "stock.exe"


def main() -> None:
    app = Application(backend="win32").connect(path=PROCESS)
    win = app.top_window()
    print(f"النافذة العليا: text={win.window_text()!r} class={win.class_name()!r}\n")

    # 1) الأزرار بالصنف المتوقّع
    print("=== ThunderRT6CommandButton ===")
    btns = win.descendants(class_name="ThunderRT6CommandButton")
    if not btns:
        print("  (لا أزرار بهذا الصنف — انظر القائمة الكاملة أدناه)")
    for b in btns:
        print(f"  text={b.window_text()!r}  rect={b.rectangle()}")

    # 2) كل العناصر ذات النص (احتياط: قد يكون صنف الزر مختلفًا على الجهاز)
    print("\n=== كل العناصر ذات النص (النص | الصنف) ===")
    for c in win.descendants():
        t = (c.window_text() or "").strip()
        if t:
            print(f"  {t!r:40}  {c.class_name()!r}")


if __name__ == "__main__":
    main()
