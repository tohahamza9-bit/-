"""
أداة استخراج auto_id لشاشتَي «بيع عملة» و«شراء عملة» (ملحق ب-1، §11.3).

تُشغَّل على الجهاز الفرعي وبرنامج MONEYADO **مفتوح على الشاشة المطلوبة**. تقرأ عناصر
الشاشة عبر pywinauto (backend=win32) وتُخرج:
  1) تفريغًا خامًا كاملًا (print_control_identifiers) → artifacts/<screen>_identifiers.txt
  2) جدول عناصر مبسّط (auto_id/الصنف/النوع/النص/الموقع) → للطباعة على الشاشة
  3) هيكل JSON مبدئي (scaffold) بالعناصر المكتشفة → artifacts/<screen>_fields_scaffold.json
     يربط الموظّف كل عنصر بدوره (foreign_account/customer/...) وينسخه إلى config/moneyado_fields.json

⚠️ قراءة فقط: لا تكتب في أي خانة، لا تضغط أي زر. آمنة تمامًا على النظام الحيّ.

الاستخدام (على الجهاز الفرعي):
    python tools/extract_auto_ids.py --screen sell     # شاشة البيع مفتوحة
    python tools/extract_auto_ids.py --screen buy      # شاشة الشراء مفتوحة
    python tools/extract_auto_ids.py --screen sell --title-re ".*بيع عملة.*"
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"

# عناوين النوافذ الافتراضية (تُطابق config/moneyado_fields.example.json)
DEFAULT_TITLES = {
    "sell": ".*بيع عملة.*",
    "buy": ".*شراء عملة.*",
}

# أدوار الخانات المطلوبة لكل شاشة (لتذكير الموظّف عند الربط اليدوي)
FIELD_ROLES = {
    "sell": [
        "foreign_account", "reference_number", "customer", "customer_name_display",
        "foreign_amount", "currency_type", "rate_multiply", "rate_divide",
        "commission_rate", "commission", "country", "payment_method", "notes",
    ],
    "buy": [
        "foreign_account", "transaction_number", "currency_type", "rate", "quantity",
        "net_amount", "commission", "customer", "country", "payment_method", "notes",
    ],
}


def _require_pywinauto():
    try:
        from pywinauto import Application  # noqa: F401
        return Application
    except ImportError:
        sys.exit(
            "pywinauto غير مثبّت. ثبّت التبعيات على الجهاز الفرعي:\n"
            "    pip install pywinauto pywin32\n"
            "ثم أعد التشغيل مع برنامج MONEYADO مفتوحًا على الشاشة المطلوبة."
        )


def _collect_controls(window) -> list[dict]:
    """يجمع عناصر الشاشة في قائمة مبسّطة قابلة للقراءة (بلا أي تفاعل)."""
    controls: list[dict] = []
    for ctrl in window.descendants():
        try:
            info = ctrl.element_info
            texts = [t for t in (ctrl.window_text(),) if t]
            controls.append({
                "auto_id": getattr(info, "automation_id", "") or getattr(info, "control_id", "") or "",
                "class_name": info.class_name or "",
                "control_type": getattr(info, "control_type", "") or type(ctrl).__name__,
                "text": (texts[0] if texts else "")[:40],
                "rect": str(info.rectangle),
            })
        except Exception as exc:  # عنصر متعذّر — نسجّله ونكمل (لا نبتلع بصمت)
            controls.append({"auto_id": "", "class_name": "?", "control_type": "?",
                             "text": f"<تعذّر قراءته: {exc}>", "rect": ""})
    return controls


def _print_table(controls: list[dict]) -> None:
    print(f"\n{'#':>3}  {'auto_id':<18} {'class_name':<20} {'control_type':<16} {'text'}")
    print("-" * 90)
    for i, c in enumerate(controls):
        print(f"{i:>3}  {c['auto_id']:<18} {c['class_name']:<20} {c['control_type']:<16} {c['text']}")


def _scaffold(screen: str, controls: list[dict]) -> dict:
    """هيكل JSON مبدئي: كل الأدوار المطلوبة بقيمة auto_id=null + قائمة العناصر المكتشفة للربط."""
    return {
        "_ملاحظة": f"هيكل مبدئي لشاشة «{screen}». اربط كل دور بـ auto_id المناسب من discovered_controls "
                   "ثم انسخ القيم إلى config/moneyado_fields.json.",
        "roles_to_fill": {role: {"auto_id": None} for role in FIELD_ROLES.get(screen, [])},
        "discovered_controls": controls,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="استخراج auto_id لشاشات MONEYADO (قراءة فقط).")
    parser.add_argument("--screen", choices=["sell", "buy"], required=True, help="نوع الشاشة المفتوحة")
    parser.add_argument("--title-re", default=None, help="نمط عنوان النافذة (يتجاوز الافتراضي)")
    args = parser.parse_args()

    Application = _require_pywinauto()
    title_re = args.title_re or DEFAULT_TITLES[args.screen]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    print(f"الاتصال بنافذة: {title_re} (backend=win32) ...")
    try:
        app = Application(backend="win32").connect(title_re=title_re, timeout=10)
        window = app.window(title_re=title_re)
    except Exception as exc:
        sys.exit(f"تعذّر الاتصال بالنافذة «{title_re}»: {exc}\n"
                 f"تأكّد أن شاشة «{args.screen}» مفتوحة في MONEYADO.")

    # (1) التفريغ الخام الكامل
    raw = io.StringIO()
    with contextlib.redirect_stdout(raw):
        window.print_control_identifiers()
    raw_path = ARTIFACTS / f"{args.screen}_identifiers.txt"
    raw_path.write_text(raw.getvalue(), encoding="utf-8")
    print(f"[1] التفريغ الخام محفوظ: {raw_path}")

    # (2) جدول مبسّط على الشاشة
    controls = _collect_controls(window)
    _print_table(controls)

    # (3) هيكل JSON مبدئي
    scaffold = _scaffold(args.screen, controls)
    scaffold_path = ARTIFACTS / f"{args.screen}_fields_scaffold.json"
    scaffold_path.write_text(json.dumps(scaffold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[3] هيكل JSON مبدئي محفوظ: {scaffold_path}")
    print("\nالخطوة التالية: اربط كل دور في roles_to_fill بـ auto_id من discovered_controls،")
    print("ثم انقل القيم إلى config/moneyado_fields.json (§11.3). لا تخمّن — دقّة الربط إلزامية.")


if __name__ == "__main__":
    main()
