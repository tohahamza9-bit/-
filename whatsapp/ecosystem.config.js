module.exports = {
  apps: [{
    name: "moneyado-wa",
    script: "src/index.js",
    node_args: "--env-file=../.env",
    cwd: "C:\\Users\\TEBA\\Desktop\\moneyado-bot\\whatsapp",
    max_restarts: 10,
    min_uptime: "10s",
    restart_delay: 3000,
    watch: false
  }]
}
