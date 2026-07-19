// إشراف pm2 على شقّي البوت: جسر واتساب (Node) + الكيرنل (Python/uvicorn).
// السبب: pm2 يعيد التشغيل تلقائيًّا عند أي انقطاع (انهيار/شبكة/انقطاع غير معروف)، و«pm2 save»
// يحفظ القائمة لتُستعاد بـ«pm2 resurrect» عند الإقلاع (مهمّة مجدولة — pm2 startup لا يعمل على ويندوز).
//
// الإيقاف اليدويّ الصحيح: pm2 stop moneyado-wa / moneyado-kernel  (لا taskkill على node.exe —
// قتل الصور يقتل عفريت pm2 معه فلا يبقى مَن يعيد التشغيل، ولا يُسجَّل شيء في pm2.log).
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
    },
    {
      // الكيرنل: يُنفَّذ ببايثون البيئة الافتراضية مباشرةً (interpreter: none = شغّل الملف كما هو).
      name: "moneyado-kernel",
      script: "C:\\Users\\TEBA\\Desktop\\moneyado-bot\\.venv\\Scripts\\python.exe",
      args: "-m uvicorn core.app:get_app --factory --host 0.0.0.0 --port 8000",
      interpreter: "none",
      cwd: "C:\\Users\\TEBA\\Desktop\\moneyado-bot",
      max_restarts: 10,
      min_uptime: "20s",       // أطول من الجسر: الإقلاع يشمل Mongo + الفهارس
      restart_delay: 5000,
      kill_timeout: 10000,     // مهلة إطفاء رشيق (§12) قبل القتل — لا تُقطع أثناء كتابة حوالة
      watch: false
    }
  ]
}
