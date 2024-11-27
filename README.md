# Solana Meme Token Watcher

A Python script that monitors Solana blockchain for potential pump and dump opportunities in new token listings.

## Features

- Real-time monitoring of new token listings on Solana
- Detection of potential pump and dump patterns
- Discord webhook notifications
- Configurable risk parameters
- Automatic tracking of suspicious token activity

## Setup

1. Clone the repository
2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in your configuration:
```bash
cp .env.example .env
```

5. Edit `.env` file with your:
   - Solana RPC URL
   - Discord Webhook URL
   - Desired configuration parameters

## Usage

Run the script:
```bash
python meme_watcher.py
```

## Configuration

Adjust the following parameters in `.env`:
- `SOLANA_RPC_URL`: Your Solana RPC endpoint
- `DISCORD_WEBHOOK_URL`: Discord webhook for notifications
- `MINIMUM_LIQUIDITY_SOL`: Minimum liquidity threshold in SOL
- `PRICE_INCREASE_THRESHOLD`: Price increase percentage to trigger alert
- `MONITORING_INTERVAL`: Time between checks in seconds

## Disclaimer

This tool is for educational purposes only. Cryptocurrency trading involves substantial risk. Always do your own research and never invest more than you can afford to lose.
