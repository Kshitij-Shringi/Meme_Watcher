import os
import sys
import time
import json
import asyncio
import logging
import aiohttp
import base58
from typing import Dict, List, Optional, Tuple
from abc import ABC, abstractmethod
from datetime import datetime
from discord_webhook import DiscordWebhook, DiscordEmbed
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
import signal
import numpy as np

# Load environment variables
load_dotenv()

# Set up logging to file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('meme_watcher.log'),
    ]
)
logger = logging.getLogger(__name__)

# Set up console output
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
console_logger = logging.getLogger('console')
console_logger.addHandler(console_handler)
console_logger.setLevel(logging.INFO)

class DataSource(ABC):
    """Abstract base class for data sources"""
    
    @abstractmethod
    async def get_top_pairs(self) -> List[Dict]:
        """Get top traded pairs"""
        pass
        
    @abstractmethod
    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        """Get pairs for specific token"""
        pass

class DexScreenerAPI(DataSource):
    """DexScreener API data source"""
    
    def __init__(self, rate_limit: float = 1.0):
        self.last_call = 0
        self.rate_limit = rate_limit
        self.search_terms = [
            'safe', 'inu', 'elon', 'baby', 'gem', 'moon', 'mini', 'doge',
            'pepe', 'wojak', 'chad', 'meme', 'shib', 'cat', 'ai'
        ]
        self.dex_names = ['raydium', 'orca', 'jupiter']
        
    async def _rate_limited_call(self, session: aiohttp.ClientSession, url: str, retries: int = 3) -> Optional[Dict]:
        """Make a rate-limited API call with retries and custom headers"""
        current_time = time.time()
        time_since_last_call = current_time - self.last_call
        
        if time_since_last_call < self.rate_limit:
            await asyncio.sleep(self.rate_limit - time_since_last_call)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        for attempt in range(retries):
            try:
                self.last_call = time.time()
                console_logger.info(f"📡 Calling API: {url}")
                
                async with session.get(url, headers=headers, timeout=30) as response:
                    if response.status == 404:
                        console_logger.warning(f"404 Not Found: {url}")
                        return {'pairs': []}
                    elif response.status == 429:  # Rate limit
                        wait_time = float(response.headers.get('Retry-After', 5))
                        console_logger.warning(f"Rate limited, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    elif response.status != 200:
                        console_logger.error(f"API error: Status {response.status} for {url}")
                        if attempt < retries - 1:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
                        return {'pairs': []}
                    
                    data = await response.json()
                    if not data or not isinstance(data, dict):
                        return {'pairs': []}
                        
                    pairs = data.get('pairs', [])
                    if pairs and len(pairs) > 0:
                        pairs_count = len(pairs)
                        console_logger.info(f"✅ Got {pairs_count} pairs from {url}")
                        # Log first pair as sample
                        sample_pair = pairs[0]
                        console_logger.info(f"Sample pair: {sample_pair.get('baseToken', {}).get('symbol')} - "
                                         f"Price: ${float(sample_pair.get('priceUsd', 0)):.8f}, "
                                         f"Liquidity: ${int(float(sample_pair.get('liquidity', {}).get('usd', 0))):,}")
                    return data
                    
            except asyncio.TimeoutError:
                console_logger.error(f"Timeout error for {url}")
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
            except Exception as e:
                console_logger.error(f"Error calling {url}: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                
        return {'pairs': []}
        
    async def get_top_pairs(self) -> List[Dict]:
        """Get top traded pairs"""
        all_pairs = []
        
        async with aiohttp.ClientSession() as session:
            # Search by keywords
            for term in self.search_terms:
                search_url = f"https://api.dexscreener.com/latest/dex/search?q={term}%20sol"
                data = await self._rate_limited_call(session, search_url)
                if data and 'pairs' in data:
                    # Filter pairs
                    filtered_pairs = []
                    for pair in data['pairs']:
                        try:
                            # Basic validation
                            if not all(k in pair for k in ['baseToken', 'priceUsd', 'liquidity', 'volume']):
                                continue
                                
                            # Get metrics
                            liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                            volume_24h = float(pair.get('volume', {}).get('h24', 0))
                            price_usd = float(pair.get('priceUsd', 0))
                            
                            # Skip if price is too high (likely not a meme token)
                            if price_usd > 1.0:
                                continue
                                
                            # Skip if liquidity is too low or too high
                            if liquidity < 5000 or liquidity > 1000000:
                                continue
                                
                            # Skip if 24h volume is too low
                            if volume_24h < 1000:
                                continue
                                
                            # Check price changes
                            price_changes = pair.get('priceChange', {})
                            m5 = abs(float(price_changes.get('m5', 0)))
                            h1 = abs(float(price_changes.get('h1', 0)))
                            h24 = abs(float(price_changes.get('h24', 0)))
                            
                            # Skip if no recent price movement
                            if m5 < 1 and h1 < 5 and h24 < 20:
                                continue
                                
                            # Get transaction counts
                            txns = pair.get('txns', {}).get('h1', {})
                            buys = int(txns.get('buys', 0))
                            sells = int(txns.get('sells', 0))
                            
                            # Skip if no recent transactions
                            if buys + sells < 5:
                                continue
                                
                            filtered_pairs.append(pair)
                            
                        except (TypeError, ValueError, KeyError):
                            continue
                            
                    if filtered_pairs:
                        all_pairs.extend(filtered_pairs)
                        console_logger.info(f"✅ Found {len(filtered_pairs)} potential pairs for '{term}'")
                        
        return all_pairs

    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        """Get pairs for specific token"""
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            data = await self._rate_limited_call(session, url)
            return data.get('pairs', []) if data else []

class BirdeyelabsAPI(DataSource):
    """Birdeye API data source"""
    
    def __init__(self, api_key: str, rate_limit: float = 1.0):
        self.api_key = api_key
        self.last_call = 0
        self.rate_limit = rate_limit
        self.headers = {
            "X-API-KEY": api_key,
            "Accept": "application/json"
        }
        self.enabled = bool(api_key and api_key != 'your_birdeye_api_key_here')
        if not self.enabled:
            logger.warning("Birdeye API disabled: No valid API key provided")
        
    async def _rate_limited_call(self, session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
        if not self.enabled:
            return None
            
        current_time = time.time()
        time_since_last_call = current_time - self.last_call
        
        if time_since_last_call < self.rate_limit:
            await asyncio.sleep(self.rate_limit - time_since_last_call)
        
        self.last_call = time.time()
        try:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 429:  # Rate limit
                    logger.warning("Birdeye API rate limit reached")
                    await asyncio.sleep(5)  # Wait before retry
                    return None
                elif response.status != 200:
                    if response.status != 404:  # Don't log 404s as errors
                        logger.error(f"Birdeye API error: {response.status}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"Birdeye API error: {e}")
            return None

    async def get_top_pairs(self) -> List[Dict]:
        if not self.enabled:
            return []
            
        async with aiohttp.ClientSession() as session:
            try:
                # First get top tokens
                data = await self._rate_limited_call(
                    session,
                    "https://public-api.birdeye.so/public/tokenlist?sort_by=v24hUSD&offset=0&limit=100"
                )
                
                if not data or 'data' not in data or 'tokens' not in data['data']:
                    return []
                    
                # Get detailed info for each token
                pairs = []
                for token in data['data']['tokens'][:30]:  # Limit to top 30
                    token_data = await self._rate_limited_call(
                        session,
                        f"https://public-api.birdeye.so/public/token_price?address={token['address']}"
                    )
                    if token_data and 'data' in token_data:
                        price_data = token_data['data']
                        pairs.append({
                            'baseToken': {
                                'address': token['address'],
                                'name': token.get('name', 'Unknown'),
                                'symbol': token.get('symbol', 'Unknown')
                            },
                            'priceUsd': price_data.get('value', 0),
                            'liquidity': {'usd': token.get('liquidity', 0)},
                            'volume': {'h24': token.get('v24hUSD', 0)},
                            'priceChange': {
                                'h1': price_data.get('h1', 0),
                                'm5': price_data.get('m5', 0)
                            }
                        })
                return pairs
            except Exception as e:
                logger.error(f"Error fetching Birdeye top pairs: {e}")
                return []
            
    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        if not self.enabled:
            return []
            
        async with aiohttp.ClientSession() as session:
            try:
                price_data = await self._rate_limited_call(
                    session,
                    f"https://public-api.birdeye.so/public/token_price?address={token_address}"
                )
                
                token_data = await self._rate_limited_call(
                    session,
                    f"https://public-api.birdeye.so/public/token?address={token_address}"
                )
                
                if not price_data or not token_data or 'data' not in price_data or 'data' not in token_data:
                    return []
                    
                price_info = price_data['data']
                token_info = token_data['data']
                
                return [{
                    'baseToken': {
                        'address': token_address,
                        'name': token_info.get('name', 'Unknown'),
                        'symbol': token_info.get('symbol', 'Unknown')
                    },
                    'priceUsd': price_info.get('value', 0),
                    'liquidity': {'usd': token_info.get('liquidity', 0)},
                    'volume': {'h24': token_info.get('v24hUSD', 0)},
                    'priceChange': {
                        'h1': price_info.get('h1', 0),
                        'm5': price_info.get('m5', 0)
                    }
                }]
            except Exception as e:
                logger.error(f"Error fetching Birdeye token data: {e}")
                return []

class JupiterAPI(DataSource):
    """Jupiter API data source"""
    
    def __init__(self, rate_limit: float = 1.0):
        self.last_call = 0
        self.rate_limit = rate_limit
        
    async def _rate_limited_call(self, session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
        current_time = time.time()
        time_since_last_call = current_time - self.last_call
        
        if time_since_last_call < self.rate_limit:
            await asyncio.sleep(self.rate_limit - time_since_last_call)
        
        self.last_call = time.time()
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Jupiter API error: {response.status}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"Jupiter API error: {e}")
            return None

    async def get_top_pairs(self) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            data = await self._rate_limited_call(
                session,
                "https://price.jup.ag/v4/price?ids=SOL"
            )
            return data.get('data', []) if data else []
            
    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            data = await self._rate_limited_call(
                session,
                f"https://price.jup.ag/v4/price?ids={token_address}"
            )
            return [data.get('data', {})] if data and data.get('data') else []

class DataAggregator:
    """Aggregates data from multiple sources"""
    
    def __init__(self):
        birdeye_key = os.getenv('BIRDEYE_API_KEY', '')
        self.sources: List[DataSource] = [
            DexScreenerAPI(),
            BirdeyelabsAPI(birdeye_key),
            JupiterAPI()
        ]
        
    async def get_aggregated_pairs(self) -> List[Dict]:
        """Get pairs from all sources and aggregate them"""
        all_pairs = []
        
        for source in self.sources:
            try:
                pairs = await source.get_top_pairs()
                if pairs:
                    filtered_pairs = self._filter_pairs(pairs)
                    if filtered_pairs:
                        console_logger.info(f"Got {len(filtered_pairs)} filtered pairs from {source.__class__.__name__}")
                        all_pairs.extend(filtered_pairs)
            except Exception as e:
                logger.error(f"Error getting pairs from {source.__class__.__name__}: {e}")
                
        deduplicated = self._deduplicate_pairs(all_pairs)
        if deduplicated:
            console_logger.info(f"Total unique pairs after filtering: {len(deduplicated)}")
        return deduplicated
        
    def _filter_pairs(self, pairs: List[Dict]) -> List[Dict]:
        """Filter pairs based on various criteria"""
        filtered = []
        for pair in pairs:
            try:
                # Skip pairs without required data
                if not all(k in pair for k in ['baseToken', 'priceUsd', 'liquidity', 'volume']):
                    continue
                    
                # Skip if token symbol contains common non-meme terms
                symbol = pair.get('baseToken', {}).get('symbol', '').lower()
                skip_terms = ['usd', 'eth', 'btc', 'usdc', 'usdt', 'dai', 'wrapped']
                if any(term in symbol for term in skip_terms):
                    continue
                    
                # Get basic metrics
                liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                volume_24h = float(pair.get('volume', {}).get('h24', 0))
                price_usd = float(pair.get('priceUsd', 0))
                
                # Apply filters
                if (5000 <= liquidity <= 1000000 and  # Reasonable liquidity range
                    volume_24h >= 1000 and           # Minimum volume
                    price_usd <= 1.0):               # Low price (typical for meme tokens)
                    
                    filtered.append(pair)
                    
            except (TypeError, ValueError, KeyError):
                continue
                
        return filtered
        
    async def get_token_data(self, token_address: str) -> List[Dict]:
        """Get token data from all sources"""
        all_data = []
        
        for source in self.sources:
            try:
                data = await source.get_token_pairs(token_address)
                if data:
                    all_data.extend(data)
            except Exception as e:
                logger.debug(f"Error getting token data from {source.__class__.__name__}: {e}")
                
        return self._deduplicate_pairs(all_data)
        
    def _deduplicate_pairs(self, pairs: List[Dict]) -> List[Dict]:
        """Remove duplicate pairs based on pair address"""
        seen_pairs = {}
        for pair in pairs:
            if not pair:
                continue
            pair_address = pair.get('pairAddress')
            if pair_address:
                if pair_address not in seen_pairs:
                    seen_pairs[pair_address] = pair
                else:
                    # Keep pair with higher liquidity if duplicate
                    existing_liq = float(seen_pairs[pair_address].get('liquidity', {}).get('usd', 0))
                    new_liq = float(pair.get('liquidity', {}).get('usd', 0))
                    if new_liq > existing_liq:
                        seen_pairs[pair_address] = pair
                        
        return list(seen_pairs.values())

class TokenAnalyzer:
    def __init__(self):
        self.rsi_period = 14
        self.rsi_overbought = 70
        self.rsi_oversold = 30
        self.volume_mcap_ratio_min = 0.1  # Min volume/mcap ratio
        self.price_history = {}  # Store price history for technical analysis
        
    def calculate_rsi(self, prices):
        """Calculate RSI for given price series"""
        deltas = np.diff(prices)
        seed = deltas[:self.rsi_period+1]
        up = seed[seed >= 0].sum()/self.rsi_period
        down = -seed[seed < 0].sum()/self.rsi_period
        rs = up/down
        rsi = np.zeros_like(prices)
        rsi[:self.rsi_period] = 100. - 100./(1.+rs)

        for i in range(self.rsi_period, len(prices)):
            delta = deltas[i-1]
            if delta > 0:
                upval = delta
                downval = 0.
            else:
                upval = 0.
                downval = -delta

            up = (up*(self.rsi_period-1) + upval)/self.rsi_period
            down = (down*(self.rsi_period-1) + downval)/self.rsi_period
            rs = up/down
            rsi[i] = 100. - 100./(1.+rs)
        return rsi[-1]

    def analyze_token(self, token_data, price_history):
        """
        Comprehensive token analysis
        Returns: score (0-1), reasons list
        """
        score = 0
        reasons = []
        
        # Price momentum
        if len(price_history) >= self.rsi_period:
            rsi = self.calculate_rsi(price_history)
            if 40 <= rsi <= 60:  # Sweet spot for entry
                score += 0.2
                reasons.append(f"RSI in optimal range: {rsi:.1f}")
            elif rsi < self.rsi_oversold:
                score += 0.15
                reasons.append(f"Oversold RSI: {rsi:.1f}")
        
        # Volume analysis
        volume_24h = float(token_data.get('volume', {}).get('h24', 0))
        mcap = float(token_data.get('marketCap', 0))
        if mcap > 0:
            vol_mcap_ratio = volume_24h / mcap
            if vol_mcap_ratio > self.volume_mcap_ratio_min:
                score += 0.2
                reasons.append(f"Strong volume/mcap ratio: {vol_mcap_ratio:.3f}")
        
        # Price change analysis
        changes = token_data.get('priceChange', {})
        m5 = float(changes.get('m5', 0))
        h1 = float(changes.get('h1', 0))
        
        if 5 <= m5 <= 20:  # Sweet spot for 5m change
            score += 0.2
            reasons.append(f"Optimal 5m change: {m5:+.1f}%")
        
        if -5 <= h1 <= 15:  # Not overextended on 1h
            score += 0.2
            reasons.append(f"Healthy 1h trend: {h1:+.1f}%")
        
        # Liquidity check
        liquidity = float(token_data.get('liquidity', {}).get('usd', 0))
        if liquidity > 5000:  # Minimum $5k liquidity
            score += 0.2
            reasons.append(f"Strong liquidity: ${liquidity:,.0f}")
            
        return score, reasons

class TradeOpportunityFinder:
    def __init__(self):
        self.discord_webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
        self.min_liquidity = float(os.getenv('MINIMUM_LIQUIDITY_USD', 10000))
        self.volume_spike_threshold = float(os.getenv('VOLUME_SPIKE_THRESHOLD', 200))
        self.price_pump_threshold = float(os.getenv('PRICE_PUMP_THRESHOLD', 30))
        self.monitoring_interval = int(os.getenv('MONITORING_INTERVAL', 60))
        self.tracked_tokens = {}
        self.token_metrics_history = {}
        self.data_aggregator = DataAggregator()
        self.is_running = True
        self.last_api_call = 0
        self.api_rate_limit = 1.0
        self.alert_cooldown = {}
        self.alert_cooldown_period = 300
        
    def run(self):
        """Start the monitoring process"""
        asyncio.run(self.monitor_opportunities())
        
    async def monitor_opportunities(self):
        """Monitor for trading opportunities"""
        while self.is_running:
            try:
                console_logger.info("🔍 Scanning for opportunities...")
                async with aiohttp.ClientSession() as session:
                    pairs = await self.data_aggregator.get_aggregated_pairs()
                    
                    if not pairs:
                        console_logger.warning("⚠️ No pairs found in this scan")
                        await asyncio.sleep(self.monitoring_interval)
                        continue
                    
                    for pair in pairs:
                        if not self.is_running:
                            break
                            
                        token_address = pair.get('baseToken', {}).get('address')
                        token_symbol = pair.get('baseToken', {}).get('symbol')
                        
                        if not token_address or not token_symbol:
                            continue
                            
                        # Skip if token was recently alerted
                        if not self.should_send_alert(token_address, token_symbol):
                            continue
                            
                        # Analyze the pair
                        signals = await self.analyze_trading_signals(token_address, [pair])
                        
                        if signals and signals['is_potential_pump']:
                            metrics = signals['metrics']
                            
                            # Format alert message
                            message = f"""🎯 **Potential Token Alert: {token_symbol}**

💰 **Price**: ${metrics['price_usd']:.8f}
📊 **Price Changes**:
• 5m: {metrics['price_change_5m']:+.1f}%
• 15m: {metrics['price_change_15m']:+.1f}%
• 1h: {metrics['price_change_1h']:+.1f}%

💎 **Market Stats**:
• Liquidity: ${metrics['liquidity']:,.0f}
• 24h Volume: ${metrics['volume_24h']:,.0f}
• Buys/Sells (1h): {metrics['buys']}/{metrics['sells']}

⚡ **Signals**:
{chr(10).join(f"• {reason}" for reason in signals['reasons'])}

🔗 **Quick Links**:
• [DexScreener](https://dexscreener.com/solana/{token_address})
• [Birdeye](https://birdeye.so/token/{token_address}?chain=solana)
• [Trade on Raydium](https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token_address})"""

                            # Send to Discord
                            if self.discord_webhook_url:
                                webhook = DiscordWebhook(url=self.discord_webhook_url, content=message)
                                await asyncio.to_thread(webhook.execute)
                            
                            # Also print to console
                            console_logger.info("\n" + message + "\n")
                            
                await asyncio.sleep(self.monitoring_interval)
                
            except Exception as e:
                console_logger.error(f"Error in monitor_opportunities: {str(e)}")
                logger.exception("Error in monitor_opportunities")
                await asyncio.sleep(self.monitoring_interval)

    def should_send_alert(self, token_address, token_symbol):
        """Check if we should send an alert for this token"""
        # Skip native SOL/SOLANA tokens
        if token_symbol.upper() in ['SOL', 'SOLANA']:
            return False
            
        current_time = time.time()
        if token_address in self.alert_cooldown:
            time_since_last_alert = current_time - self.alert_cooldown[token_address]
            if time_since_last_alert < self.alert_cooldown_period:
                return False
        
        self.alert_cooldown[token_address] = current_time
        return True

    async def _rate_limited_call(self, session: aiohttp.ClientSession, url: str, retries: int = 3) -> Optional[Dict]:
        """Make a rate-limited API call with retries and custom headers"""
        current_time = time.time()
        time_since_last_call = current_time - self.last_api_call
        
        if time_since_last_call < self.api_rate_limit:
            await asyncio.sleep(self.api_rate_limit - time_since_last_call)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        for attempt in range(retries):
            try:
                self.last_api_call = time.time()
                console_logger.info(f"📡 Calling API: {url}")
                
                async with session.get(url, headers=headers, timeout=30) as response:
                    if response.status == 404:
                        console_logger.error(f"404 Not Found: {url}")
                        return None
                    elif response.status == 429:  # Rate limit
                        wait_time = float(response.headers.get('Retry-After', 5))
                        console_logger.warning(f"Rate limited, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    elif response.status != 200:
                        console_logger.error(f"API error: Status {response.status} for {url}")
                        if attempt < retries - 1:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
                        return None
                    
                    data = await response.json()
                    return data
                    
            except asyncio.TimeoutError:
                console_logger.warning(f"Timeout for {url}, attempt {attempt + 1}/{retries}")
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
            except Exception as e:
                console_logger.error(f"Error calling {url}: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
        
        return None

    async def analyze_trading_signals(self, token_address: str, pairs: List[Dict]) -> Optional[Dict]:
        """Analyze trading signals for early pump opportunities"""
        try:
            if not pairs:
                return None

            pair = pairs[0]  # Use primary pair for analysis
            
            # Basic metrics
            liquidity = float(pair.get('liquidity', {}).get('usd', 0))
            volume_24h = float(pair.get('volume', {}).get('h24', 0))
            volume_1h = float(pair.get('volume', {}).get('h1', 0))
            price_usd = float(pair.get('priceUsd', 0))
            
            # Price changes
            price_change_5m = float(pair.get('priceChange', {}).get('m5', 0))
            price_change_15m = float(pair.get('priceChange', {}).get('m15', 0))
            price_change_1h = float(pair.get('priceChange', {}).get('h1', 0))
            
            # Transaction data
            txns = pair.get('txns', {}).get('h1', {})
            buys = int(txns.get('buys', 0))
            sells = int(txns.get('sells', 0))

            # Skip if liquidity is too low
            if liquidity < self.min_liquidity:
                return None

            # Initialize indicators
            indicators = {
                'is_potential_pump': False,
                'reasons': [],
                'metrics': {
                    'liquidity': liquidity,
                    'volume_24h': volume_24h,
                    'price_usd': price_usd,
                    'price_change_5m': price_change_5m,
                    'price_change_15m': price_change_15m,
                    'price_change_1h': price_change_1h,
                    'buys': buys,
                    'sells': sells
                }
            }

            # Check volume spike
            volume_liquidity_ratio = volume_1h / liquidity if liquidity > 0 else 0
            if volume_liquidity_ratio > 0.1:  # Volume > 10% of liquidity in 1h
                indicators['reasons'].append(f"Volume spike detected ({volume_liquidity_ratio:.1f}x liquidity)")

            # Check price momentum
            if price_change_5m > 5:  # >5% in 5m
                indicators['reasons'].append(f"Strong 5m momentum: +{price_change_5m:.1f}%")
            if price_change_15m > 10:  # >10% in 15m
                indicators['reasons'].append(f"Strong 15m momentum: +{price_change_15m:.1f}%")

            # Check buy pressure
            if buys + sells > 0:
                buy_ratio = buys / (buys + sells)
                if buy_ratio > 0.6:  # >60% buys
                    indicators['reasons'].append(f"High buy pressure: {buy_ratio:.0%} buys")

            # Determine if this is a potential pump
            indicators['is_potential_pump'] = len(indicators['reasons']) >= 2

            return indicators

        except Exception as e:
            logger.error(f"Error in analyze_trading_signals: {e}")
            return None

if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s'
    )
    
    console_logger.info("🚀 Starting Meme Token Watcher...")
    console_logger.info("Press Ctrl+C to stop")
    console_logger.info("")
    
    # Create and start the finder
    finder = TradeOpportunityFinder()
    
    try:
        # Start monitoring
        finder.run()
    except KeyboardInterrupt:
        console_logger.info("\n👋 Shutting down gracefully...")
    except Exception as e:
        console_logger.error(f"❌ Error: {str(e)}")
        raise
