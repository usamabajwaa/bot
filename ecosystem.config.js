module.exports = {
  apps: [{
    name: 'mgc-live-trader',
    script: 'live_trader.py',
    interpreter: 'python',
    args: '--config config_production.json',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    log_file: './logs/pm2-combined.log',
    time: true
  }]
};
