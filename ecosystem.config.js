module.exports = {
  apps: [{
    name: 'mgc-live-trader',
    script: 'live_trader.py',
    interpreter: 'python',
    args: '--config config_production.json --credentials credentials.json --interval 30',
    cwd: './',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    log_file: './logs/pm2-combined.log',
    time: true,
    merge_logs: true,
    env: {
      NODE_ENV: 'production',
      PYTHONUNBUFFERED: '1'
    }
  }]
};

