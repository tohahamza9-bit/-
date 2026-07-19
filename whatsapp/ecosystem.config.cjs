// إشراف pm2 على **جسر واتساب وحده** (Node). مستقرّ عليه منذ البداية.
//
// 🔴 الكيرنل (Python/uvicorn) **أُخرِج من pm2 عمدًا (2026-07-19)**: pm2 على ويندوز كان يقتله
//    كل دقيقة تقريبًا بالخروج 3221225786 (0xC000013A = STATUS_CONTROL_C_EXIT) — إشارة CTRL_C
//    لمجموعة العمليات، مع «No matching pid found» في pidusage لأن python.exe في .venv وسيطٌ
//    يُعيد التنفيذ بمفسّر آخر فيضيع تتبّع pm2 للـPID. النتيجة: ↺=9 وحلقة تقلّب.
//    البديل المعتمد: **مهمّة ويندوز مجدولة اسمها `moneyado-kernel`** (مُشرِف النظام الأصليّ،
//    بلا إشارات مجموعة، تعمل عند تسجيل الدخول). لا تُعِد الكيرنل هنا.
//
// إدارة الكيرنل: schtasks /Run|/End /TN moneyado-kernel   (أو Task Scheduler)
// الإيقاف اليدويّ للجسر: pm2 stop moneyado-wa  (لا taskkill على node.exe — قتل الصور يقتل
// عفريت pm2 معه فلا يبقى مَن يعيد التشغيل، ولا يُسجَّل شيء في pm2.log).
module.exports = {
  apps: [
    {
      name: "moneyado-wa",
      script: "src/index.js",
      node_args: "--env-file=../.env",
      cwd: "C:\\Users\\TEBA\\Desktop\\moneyado-bot\\whatsapp",
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 3000,
      watch: false
    }
  ]
}
