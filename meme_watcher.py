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
                    if 'pairs' in data:
                        pairs_count = len(data['pairs'])
                        console_logger.info(f"✅ Got {pairs_count} pairs from {url}")
                        if pairs_count > 0:
                            # Log first pair as sample
                            sample_pair = data['pairs'][0]
                            console_logger.info(f"Sample pair: {sample_pair.get('baseToken', {}).get('symbol')} - "
                                             f"Price: ${float(sample_pair.get('priceUsd', 0)):.8f}, "
                                             f"Liquidity: ${float(sample_pair.get('liquidity', {}).get('usd', 0)):,.0f}")
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

    async def get_top_pairs(self) -> List[Dict]:
        """Get potential pump tokens"""
        async with aiohttp.ClientSession() as session:
            # Focus on recent and low-cap pairs
            search_strategies = [
                # Recent pairs from major DEXes
                "https://api.dexscreener.com/latest/dex/pairs/solana/raydium",
                "https://api.dexscreener.com/latest/dex/pairs/solana/orca",
                "https://api.dexscreener.com/latest/dex/pairs/solana/jupiter",
                
                # General pair searches
                "https://api.dexscreener.com/latest/dex/pairs/solana",
                
                # Random token searches (high volatility keywords)
                "https://api.dexscreener.com/latest/dex/search?q=baby%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=mini%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=moon%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=gem%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=safe%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=elon%20sol",
                "https://api.dexscreener.com/latest/dex/search?q=inu%20sol",
            ]
            
            # Gather pairs concurrently
            async def fetch_pairs(url):
                data = await self._rate_limited_call(session, url)
                if data and 'pairs' in data:
                    return data['pairs']
                return []
            
            tasks = [fetch_pairs(url) for url in search_strategies]
            all_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            all_pairs = []
            seen_pairs = set()
            
            # Process results
            for pairs in all_results:
                if isinstance(pairs, Exception):
                    continue
                    
                for pair in pairs:
                    if not isinstance(pair, dict):
                        continue
                        
                    if pair.get('chainId') != 'solana':
                        continue
                        
                    pair_address = pair.get('pairAddress')
                    if not pair_address or pair_address in seen_pairs:
                        continue
                    
                    # Focus on newer pairs with some activity
                    try:
                        # Get basic metrics
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        volume_24h = float(pair.get('volume', {}).get('h24', 0))
                        price_usd = float(pair.get('priceUsd', 0))
                        created_at = float(pair.get('pairCreatedAt', 0))
                        
                        # Skip if too old (> 30 days)
                        if time.time() - created_at > 30 * 24 * 3600:
                            continue
                            
                        # Skip if liquidity too high (likely established token)
                        if liquidity > 100000:  # $100k max liquidity
                            continue
                            
                        # Must have some minimal liquidity and volume
                        if liquidity < 1000 or volume_24h < 100:  # $1k min liquidity, $100 min volume
                            continue
                            
                        # Check price movement
                        price_change_5m = abs(float(pair.get('priceChange', {}).get('m5', 0)))
                        price_change_1h = abs(float(pair.get('priceChange', {}).get('h1', 0)))
                        
                        # Skip if no recent price movement
                        if price_change_5m < 0.5 and price_change_1h < 2:  # Need some action
                            continue
                            
                        # Check transaction activity
                        txns = pair.get('txns', {}).get('h1', {})
                        buys = float(txns.get('buys', 0))
                        sells = float(txns.get('sells', 0))
                        
                        # Skip if no recent transactions
                        if buys + sells < 5:  # Need some activity
                            continue
                            
                        # Calculate buy pressure
                        buy_pressure = buys / (buys + sells) if (buys + sells) > 0 else 0
                        if buy_pressure < 0.4:  # Need some buy pressure
                            continue
                            
                        # Add to our collection
                        seen_pairs.add(pair_address)
                        all_pairs.append(pair)
                        
                    except (TypeError, ValueError):
                        continue
            
            if not all_pairs:
                logger.error("No pairs found from any endpoint")
                return []
                
            # Score pairs based on pump potential
            def pump_score(p):
                try:
                    volume = float(p.get('volume', {}).get('h1', 0))  # 1h volume
                    liquidity = float(p.get('liquidity', {}).get('usd', 0))
                    price_change_5m = abs(float(p.get('priceChange', {}).get('m5', 0)))
                    price_change_1h = abs(float(p.get('priceChange', {}).get('h1', 0)))
                    txns = p.get('txns', {}).get('h1', {})
                    buys = float(txns.get('buys', 0))
                    sells = float(txns.get('sells', 0))
                    
                    # Scoring factors:
                    volume_liquidity_ratio = volume / liquidity if liquidity > 0 else 0
                    price_momentum = (price_change_5m * 2 + price_change_1h) / 3  # Weight recent more
                    buy_pressure = buys / (buys + sells) if (buys + sells) > 0 else 0
                    
                    # Combine factors (emphasize recent activity)
                    return (volume_liquidity_ratio * 0.4 + 
                           price_momentum * 0.4 + 
                           buy_pressure * 0.2)
                except:
                    return 0
            
            # Sort by pump potential
            all_pairs.sort(key=pump_score, reverse=True)
            
            # Take top potential pump targets
            final_pairs = all_pairs[:50]  # Focus on top 50 potential pumps
            
            logger.info(f"Found {len(final_pairs)} potential pump targets")
            return final_pairs

    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            data = await self._rate_limited_call(
                session,
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            )
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
                    logger.info(f"Got {len(pairs)} pairs from {source.__class__.__name__}")
                    all_pairs.extend(pairs)
            except Exception as e:
                logger.error(f"Error getting pairs from {source.__class__.__name__}: {e}")
                
        deduplicated = self._deduplicate_pairs(all_pairs)
        logger.info(f"Total unique pairs after deduplication: {len(deduplicated)}")
        return deduplicated
        
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
            if pair and 'pairAddress' in pair:
                seen_pairs[pair['pairAddress']] = pair
        return list(seen_pairs.values())

class Position:
    def __init__(self, token_address: str, symbol: str, entry_price: float, amount_usd: float = 10.0):
        self.token_address = token_address
        self.symbol = symbol
        self.entry_price = entry_price
        self.amount_usd = amount_usd
        self.entry_time = time.time()
        self.tokens = amount_usd / entry_price if entry_price > 0 else 0
        self.exit_price = None
        self.exit_time = None
        self.profit_loss = 0
        self.profit_loss_pct = 0
        self.status = "OPEN"  # OPEN, CLOSED, STOP_LOSS
        
    def update(self, current_price: float) -> Tuple[bool, str]:
        """Update position and check if we should exit"""
        if self.status != "OPEN":
            return False, ""
            
        # Calculate current P/L
        current_value = self.tokens * current_price
        self.profit_loss = current_value - self.amount_usd
        self.profit_loss_pct = (current_value / self.amount_usd - 1) * 100
        
        # Exit signals
        time_held = time.time() - self.entry_time
        
        # 1. Quick Scalp (< 5 minutes)
        if time_held < 300:
            if self.profit_loss_pct >= 50:  # Quick 50% gain
                return True, "🎯 Quick scalp profit target hit (50%)"
                
        # 2. Short-term Hold (5-15 minutes)
        elif time_held < 900:
            if self.profit_loss_pct >= 30:  # 30% gain
                return True, "💰 Short-term profit target hit (30%)"
                
        # 3. Medium-term Hold (15-30 minutes)
        elif time_held < 1800:
            if self.profit_loss_pct >= 20:  # 20% gain
                return True, "💵 Medium-term profit target hit (20%)"
                
        # 4. Dynamic Stop Loss based on time held
        stop_loss_pct = -15  # Base stop loss
        if time_held > 900:  # After 15 minutes, tighten stop loss
            stop_loss_pct = -10
        if time_held > 1800:  # After 30 minutes, tighten more
            stop_loss_pct = -7
            
        if self.profit_loss_pct <= stop_loss_pct:
            return True, f"🛑 Stop loss hit ({stop_loss_pct}%)"
            
        # 5. Dynamic Trailing Stop based on profit level
        if self.profit_loss_pct > 50:  # In massive profit
            trailing_stop = current_price * 0.85  # 15% trailing stop
            if trailing_stop < current_price:
                return True, "🎢 Wide trailing stop hit (15%)"
        elif self.profit_loss_pct > 30:  # In good profit
            trailing_stop = current_price * 0.90  # 10% trailing stop
            if trailing_stop < current_price:
                return True, "🎢 Medium trailing stop hit (10%)"
        elif self.profit_loss_pct > 15:  # In decent profit
            trailing_stop = current_price * 0.95  # 5% trailing stop
            if trailing_stop < current_price:
                return True, "🎢 Tight trailing stop hit (5%)"
                
        # 6. Rapid Price Drop Protection
        price_change_pct = ((current_price / self.entry_price) - 1) * 100
        if price_change_pct < -10 and time_held < 300:  # 10% drop in first 5 minutes
            return True, "⚡ Rapid price drop protection triggered"
            
        # 7. Time-based exit (Max hold time)
        if time_held > 3600:  # 1 hour max hold
            return True, "⏰ Max hold time reached (1 hour)"
            
        # 8. Profit Lock-in for longer holds
        if time_held > 1800 and self.profit_loss_pct > 10:  # After 30 mins with >10% profit
            return True, "🔒 Profit locked in after extended hold"
            
        return False, ""
        
    def close(self, exit_price: float, reason: str):
        """Close the position"""
        self.exit_price = exit_price
        self.exit_time = time.time()
        self.status = "CLOSED"
        
        # Calculate final P/L
        final_value = self.tokens * exit_price
        self.profit_loss = final_value - self.amount_usd
        self.profit_loss_pct = (final_value / self.amount_usd - 1) * 100
        
        return self.format_exit_message(reason)
        
    def format_exit_message(self, reason: str) -> str:
        """Format exit message for Discord"""
        time_held = self.exit_time - self.entry_time
        hours = int(time_held // 3600)
        minutes = int((time_held % 3600) // 60)
        seconds = int(time_held % 60)
        
        return (
            f"🔄 EXIT SIGNAL: {self.symbol}\n"
            f"💰 Entry: ${self.entry_price:.8f}\n"
            f"💵 Exit: ${self.exit_price:.8f}\n"
            f"⏱ Time Held: {hours}h {minutes}m {seconds}s\n"
            f"📊 P/L: ${self.profit_loss:.2f} ({self.profit_loss_pct:+.2f}%)\n"
            f"📝 Reason: {reason}\n"
            f"🔗 Chart: https://dexscreener.com/solana/{self.token_address}"
        )

class Portfolio:
    def __init__(self, initial_balance: float = 100.0, position_size: float = 10.0):
        self.initial_balance = initial_balance
        self.position_size = position_size
        self.available_balance = initial_balance
        self.total_value = initial_balance
        self.active_positions = {}  # token_address -> Position
        self.closed_positions = []
        self.total_trades = 0
        self.winning_trades = 0
        self.total_profit_loss = 0.0
        self.peak_balance = initial_balance
        self.last_update_time = time.time()
        
        # For simulated $10 positions
        self.simulated_positions = {}  # token_address -> Position
        self.simulated_closed_positions = []
        self.simulated_total_trades = 0
        self.simulated_winning_trades = 0
        self.simulated_total_profit_loss = 0.0
        
    def can_open_position(self) -> bool:
        """Check if we have enough balance to open a new position"""
        return self.available_balance >= self.position_size
        
    def open_position(self, token_address: str, symbol: str, entry_price: float) -> Optional[Position]:
        """Open a new position if we have enough balance"""
        # Always open a simulated position
        simulated_position = Position(token_address, symbol, entry_price, 10.0)  # Always $10
        self.simulated_positions[token_address] = simulated_position
        self.simulated_total_trades += 1
        
        # Only open a real position if we have enough balance
        if not self.can_open_position():
            return simulated_position
            
        position = Position(token_address, symbol, entry_price, self.position_size)
        self.active_positions[token_address] = position
        self.available_balance -= self.position_size
        self.total_trades += 1
        return position
        
    def close_position(self, token_address: str, exit_price: float, reason: str) -> Optional[Position]:
        """Close a position and update portfolio stats"""
        # Close simulated position if it exists
        if token_address in self.simulated_positions:
            sim_position = self.simulated_positions.pop(token_address)
            sim_position.close(exit_price, reason)
            self.simulated_closed_positions.append(sim_position)
            self.simulated_total_profit_loss += sim_position.profit_loss
            if sim_position.profit_loss > 0:
                self.simulated_winning_trades += 1
        
        # Close real position if it exists
        if token_address in self.active_positions:
            position = self.active_positions.pop(token_address)
            position.close(exit_price, reason)
            self.closed_positions.append(position)
            self.available_balance += position.amount_usd + position.profit_loss
            self.total_profit_loss += position.profit_loss
            
            if position.profit_loss > 0:
                self.winning_trades += 1
            
            # Update peak balance
            self.total_value = self.available_balance + sum(
                pos.tokens * pos.entry_price for pos in self.active_positions.values()
            )
            self.peak_balance = max(self.peak_balance, self.total_value)
            
            return position
            
        return None
        
    def get_portfolio_stats(self) -> Dict:
        """Get current portfolio statistics"""
        current_time = time.time()
        time_running = current_time - self.last_update_time
        
        active_value = sum(
            pos.tokens * pos.entry_price for pos in self.active_positions.values()
        )
        
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        roi = ((self.total_value / self.initial_balance) - 1) * 100
        
        # Calculate simulated stats
        sim_win_rate = (self.simulated_winning_trades / self.simulated_total_trades * 100) if self.simulated_total_trades > 0 else 0
        sim_active_value = sum(pos.tokens * pos.entry_price for pos in self.simulated_positions.values())
        sim_total_value = sim_active_value + self.simulated_total_profit_loss
        sim_roi = (self.simulated_total_profit_loss / (10.0 * self.simulated_total_trades) * 100) if self.simulated_total_trades > 0 else 0
        
        return {
            "total_value": self.total_value,
            "available_balance": self.available_balance,
            "active_positions": len(self.active_positions),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": win_rate,
            "total_profit_loss": self.total_profit_loss,
            "roi": roi,
            "peak_balance": self.peak_balance,
            "time_running": time_running,
            # Simulated stats ($10 per trade)
            "sim_total_value": sim_total_value,
            "sim_active_positions": len(self.simulated_positions),
            "sim_total_trades": self.simulated_total_trades,
            "sim_winning_trades": self.simulated_winning_trades,
            "sim_win_rate": sim_win_rate,
            "sim_total_profit_loss": self.simulated_total_profit_loss,
            "sim_roi": sim_roi,
            "sim_total_invested": 10.0 * self.simulated_total_trades
        }
        
    def format_portfolio_message(self) -> str:
        """Format portfolio stats for Discord message"""
        stats = self.get_portfolio_stats()
        
        # Format time running
        hours = int(stats["time_running"] // 3600)
        minutes = int((stats["time_running"] % 3600) // 60)
        
        return f"""📊 **Portfolio Summary**

💰 **Real Portfolio ($100 Initial)**
• Total Value: ${stats['total_value']:.2f}
• Available Balance: ${stats['available_balance']:.2f}
• Total P/L: ${stats['total_profit_loss']:.2f} ({stats['roi']:.1f}%)
• Peak Balance: ${stats['peak_balance']:.2f}
• Active Positions: {stats['active_positions']}
• Total Trades: {stats['total_trades']}
• Win Rate: {stats['win_rate']:.1f}%

🎮 **Simulated Portfolio ($10 per Trade)**
• Total Value: ${stats['sim_total_value']:.2f}
• Total P/L: ${stats['sim_total_profit_loss']:.2f} ({stats['sim_roi']:.1f}%)
• Total Invested: ${stats['sim_total_invested']:.2f}
• Active Positions: {stats['sim_active_positions']}
• Total Trades: {stats['sim_total_trades']}
• Win Rate: {stats['sim_win_rate']:.1f}%

⏱️ Running Time: {hours}h {minutes}m"""

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
        self.portfolio = Portfolio(
            initial_balance=float(os.getenv('INITIAL_BALANCE', 100)),
            position_size=float(os.getenv('POSITION_SIZE_USD', 10))
        )

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
                    if 'pairs' in data:
                        pairs_count = len(data['pairs'])
                        console_logger.info(f"✅ Got {pairs_count} pairs from {url}")
                        if pairs_count > 0:
                            # Log first pair as sample
                            sample_pair = data['pairs'][0]
                            console_logger.info(f"Sample pair: {sample_pair.get('baseToken', {}).get('symbol')} - "
                                             f"Price: ${float(sample_pair.get('priceUsd', 0)):.8f}, "
                                             f"Liquidity: ${float(sample_pair.get('liquidity', {}).get('usd', 0)):,.0f}")
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

    async def rate_limited_api_call(self, session, url):
        """Make a rate-limited API call"""
        current_time = time.time()
        time_since_last_call = current_time - self.last_api_call
        
        if time_since_last_call < self.api_rate_limit:
            await asyncio.sleep(self.api_rate_limit - time_since_last_call)
        
        self.last_api_call = time.time()
        return await session.get(url)

    def format_number(self, num):
        """Format numbers for better readability"""
        if num >= 1_000_000:
            return f"${num/1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num/1_000:.2f}K"
        return f"${num:.2f}"

    def format_percentage(self, pct):
        """Format percentage with color indicators"""
        if pct > 0:
            return f"🟢 +{pct:.1f}%"
        elif pct < 0:
            return f"🔴 {pct:.1f}%"
        return f"⚪ {pct:.1f}%"

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

    async def analyze_trading_signals(self, token_address: str, pairs: List[Dict]) -> Optional[Dict]:
        """Analyze trading signals for early pump opportunities"""
        try:
            if not pairs:
                return None

            pair = pairs[0]
            
            try:
                # Basic metrics
                total_liquidity = sum(float(p.get('liquidity', {}).get('usd', 0)) for p in pairs)
                total_volume = sum(float(p.get('volume', {}).get('h24', 0)) for p in pairs)
                price_usd = float(pair.get('priceUsd', 0))
                
                # Price changes
                price_change_1m = float(pair.get('priceChange', {}).get('m1', 0))
                price_change_5m = float(pair.get('priceChange', {}).get('m5', 0))
                price_change_15m = float(pair.get('priceChange', {}).get('m15', 0))
                price_change_1h = float(pair.get('priceChange', {}).get('h1', 0))
                price_change_6h = float(pair.get('priceChange', {}).get('h6', 0))
                price_change_24h = float(pair.get('priceChange', {}).get('h24', 0))
                
                # Transaction data
                txns_1h = pair.get('txns', {}).get('h1', {})
                txns_24h = pair.get('txns', {}).get('h24', {})
                buys_1h = float(txns_1h.get('buys', 0))
                sells_1h = float(txns_1h.get('sells', 0))
                
            except (TypeError, ValueError) as e:
                logger.error(f"Error parsing metrics: {e}")
                return None

            # Early pump indicators
            indicators = {
                'is_potential_pump': False,
                'reasons': [],
                'risk_score': 0  # 0-100, higher = riskier
            }

            # === VOLUME ANALYSIS ===
            # Recent volume spike
            hourly_volume = total_volume / 24
            if hourly_volume > total_liquidity * 0.1:  # Volume > 10% of liquidity in an hour
                indicators['reasons'].append("Volume spike detected")
                indicators['risk_score'] += 20
            
            # Increasing volume trend
            if buys_1h + sells_1h > (buys_24h + sells_24h) / 12:  # Higher than average hourly volume
                indicators['reasons'].append("Increasing volume")
                indicators['risk_score'] += 15

            # === TRANSACTION ANALYSIS ===
            # Recent buy pressure
            if buys_1h + sells_1h >= 10:  # Minimum 10 transactions
                buy_ratio_1h = buys_1h / (buys_1h + sells_1h)
                if buy_ratio_1h > 0.65:  # >65% buys
                    indicators['reasons'].append("Strong recent buy pressure")
                    indicators['risk_score'] += 25
                elif buy_ratio_1h > 0.55:  # >55% buys
                    indicators['reasons'].append("Moderate buy pressure")
                    indicators['risk_score'] += 15

            # === PRICE ACTION ANALYSIS ===
            # Quick momentum
            if price_change_1m > 2 and price_change_5m > 5:
                indicators['reasons'].append("Quick upward movement")
                indicators['risk_score'] += 20
            
            # Building momentum
            elif price_change_5m > 3 and price_change_15m > 8:
                indicators['reasons'].append("Building momentum")
                indicators['risk_score'] += 15
            
            # Healthy growth
            elif 5 < price_change_1h < 20:
                indicators['reasons'].append("Steady growth pattern")
                indicators['risk_score'] += 10

            # === LIQUIDITY ANALYSIS ===
            if total_liquidity < 3000:  # Minimum $3k liquidity
                return None
            
            # Adjust risk based on liquidity
            if total_liquidity > 50000:
                indicators['risk_score'] -= 20
            elif total_liquidity > 20000:
                indicators['risk_score'] -= 10
            elif total_liquidity < 5000:
                indicators['risk_score'] += 20

            # === FINAL DECISION ===
            # More lenient conditions for potential pumps
            indicators['is_potential_pump'] = (
                len(indicators['reasons']) >= 2 and     # At least 2 positive indicators
                indicators['risk_score'] >= 25 and      # Decent risk/reward
                total_liquidity >= 3000 and            # Minimum liquidity
                price_change_1h > -15 and              # Not dumping hard
                (price_change_5m > 2 or                # Some recent movement
                 price_change_15m > 5 or
                 price_change_1h > 5)
            )

            if indicators['is_potential_pump']:
                return {
                    'token_address': token_address,
                    'token_name': pair.get('baseToken', {}).get('name', 'Unknown'),
                    'token_symbol': pair.get('baseToken', {}).get('symbol', 'Unknown'),
                    'pair_address': pair.get('pairAddress', ''),
                    'dex': pair.get('dexId', 'RAYDIUM'),
                    'metrics': {
                        'price': price_usd,
                        'liquidity': total_liquidity,
                        'volume_24h': total_volume,
                        'price_change_5m': price_change_5m,
                        'price_change_1h': price_change_1h,
                        'price_change_24h': price_change_24h,
                        'hourly_volume': hourly_volume,
                        'buy_ratio_1h': (buys_1h / (buys_1h + sells_1h)) * 100 if (buys_1h + sells_1h) > 0 else 0
                    },
                    'indicators': indicators
                }

            return None

        except Exception as e:
            logger.error(f"Error analyzing trading signals: {e}")
            return None

    async def send_discord_alert(self, pair: Dict):
        """Send detailed alert to Discord"""
        if not self.discord_webhook_url:
            return
            
        try:
            # Extract token info
            token = pair.get('baseToken', {})
            symbol = token.get('symbol', 'Unknown')
            address = token.get('address', '')
            
            # Convert string values to float, with error handling
            try:
                price = float(pair.get('priceUsd', '0'))
                liquidity = float(pair.get('liquidity', {}).get('usd', '0'))
                volume_24h = float(pair.get('volume', {}).get('h24', '0'))
                
                # Get price changes with proper conversion
                changes = pair.get('priceChange', {})
                m5 = float(changes.get('m5', '0'))
                m15 = float(changes.get('m15', '0'))
                h1 = float(changes.get('h1', '0'))
                h24 = float(changes.get('h24', '0'))
            except (ValueError, TypeError):
                console_logger.error(f"Error converting price data for {symbol}")
                return
                
            # Get transaction data
            txns = pair.get('txns', {}).get('h1', {})
            buys = int(txns.get('buys', 0))
            sells = int(txns.get('sells', 0))
            
            # Get analysis data
            analysis = pair.get('_analysis', {})
            pump_score = float(analysis.get('pump_score', 0))
            reason = analysis.get('reason', 'Unknown')
            
            # Format message
            message = f"""🚨 **Potential Pump Alert: {symbol}**

💰 **Price Analysis**
• Current: ${price:.8f}
• Score: {pump_score:.2f}/1.0
• Reason: {reason}

📈 **Price Changes**
• 5m: {m5:+.1f}%
• 15m: {m15:+.1f}%
• 1h: {h1:+.1f}%
• 24h: {h24:+.1f}%

📊 **Market Stats**
• Liquidity: ${liquidity:,.0f}
• 24h Volume: ${volume_24h:,.0f}
• 1h Buys: {buys}
• 1h Sells: {sells}

🔗 **Quick Links**
• [DexScreener](https://dexscreener.com/solana/{pair.get('pairAddress')})
• [Birdeye](https://birdeye.so/token/{address}?chain=solana)
• [Trade on Raydium](https://raydium.io/swap/?inputCurrency=sol&outputCurrency={address})

{self.portfolio.format_portfolio_message()}"""
            
            # Send to Discord
            webhook = DiscordWebhook(url=self.discord_webhook_url, content=message)
            response = webhook.execute()
            
            if response.status_code == 204:  # Discord returns 204 on success
                console_logger.info(f"📨 Sent Discord alert for {symbol}")
            else:
                console_logger.error(f"Failed to send Discord alert: {response.status_code}")
            
        except Exception as e:
            console_logger.error(f"Error sending Discord alert: {str(e)}")

    async def monitor_opportunities(self):
        """Monitor for trading opportunities"""
        while self.is_running:
            try:
                console_logger.info("🔍 Scanning for opportunities...")
                
                # Get potential pump candidates
                pairs = await self.get_top_pairs()
                
                if not pairs:
                    console_logger.info("⚠️  No pairs found, retrying in 60s...")
                    await asyncio.sleep(60)
                    continue
                
                # First update existing positions
                await self.update_positions(pairs)
                
                # Send portfolio update every hour
                current_time = time.time()
                if current_time - self.portfolio.last_update_time >= 3600:  # 1 hour
                    self.portfolio.last_update_time = current_time
                    if self.discord_webhook_url:
                        webhook = DiscordWebhook(
                            url=self.discord_webhook_url,
                            content=self.portfolio.format_portfolio_message()
                        )
                        await asyncio.to_thread(webhook.execute)
                
                # Then process new opportunities
                for pair in pairs:
                    try:
                        # Extract token info
                        token = pair.get('baseToken', {})
                        token_address = token.get('address')
                        symbol = token.get('symbol', 'Unknown')
                        
                        # Skip if we already have a simulated position
                        if token_address in self.portfolio.simulated_positions:
                            continue
                        
                        price = float(pair.get('priceUsd', '0'))
                        liquidity = float(pair.get('liquidity', {}).get('usd', '0'))
                        volume_24h = float(pair.get('volume', {}).get('h24', '0'))
                        
                        # Get price changes
                        changes = pair.get('priceChange', {})
                        m5 = float(changes.get('m5', '0'))
                        m15 = float(changes.get('m15', '0'))
                        h1 = float(changes.get('h1', '0'))
                        h24 = float(changes.get('h24', '0'))
                        
                        # Get analysis data
                        analysis = pair.get('_analysis', {})
                        pump_score = float(analysis.get('pump_score', 0))
                        
                        # Open simulated position
                        position = self.portfolio.open_position(token_address, symbol, price)
                        
                        # Format entry message
                        message = f"""🎯 **New Trade Alert: {symbol}**

💰 **Entry Details**
• Price: ${price:.8f}
• Position Size: $10.00
• Tokens: {10.0/price:.2f}

📈 **Price Action**
• 5m: {m5:+.1f}%
• 15m: {m15:+.1f}%
• 1h: {h1:+.1f}%
• 24h: {h24:+.1f}%

📊 **Market Stats**
• Liquidity: ${liquidity:,.0f}
• 24h Volume: ${volume_24h:,.0f}
• Score: {pump_score:.2f}/1.0

🔗 **Quick Links**
• [DexScreener](https://dexscreener.com/solana/{pair.get('pairAddress')})
• [Birdeye](https://birdeye.so/token/{token_address}?chain=solana)
• [Trade on Raydium](https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token_address})

{self.portfolio.format_portfolio_message()}"""
                        
                        # Send to Discord
                        if self.discord_webhook_url:
                            webhook = DiscordWebhook(
                                url=self.discord_webhook_url,
                                content=message
                            )
                            await asyncio.to_thread(webhook.execute)
                        
                        # Also print to console
                        console_logger.info("\n" + message + "\n")
                        
                    except Exception as e:
                        console_logger.error(f"Error processing pair {symbol}: {str(e)}")
                        continue
                
                # Wait before next scan
                await asyncio.sleep(60)
                
            except Exception as e:
                console_logger.error(f"❌ Error: {str(e)}")
                await asyncio.sleep(60)

    async def update_positions(self, current_pairs: List[Dict]):
        """Update all active positions and check for exits"""
        # Create price lookup
        price_lookup = {
            pair.get('pairAddress'): float(pair.get('priceUsd', '0'))
            for pair in current_pairs
        }
        
        # Update each position
        positions_to_remove = []
        
        for token_address, position in self.portfolio.active_positions.items():
            try:
                current_price = price_lookup.get(token_address)
                if not current_price:
                    continue
                    
                # Check for exit signal
                should_exit, reason = position.update(current_price)
                
                if should_exit:
                    # Format exit message
                    exit_message = position.format_exit_message(reason)
                    
                    # Close position and update portfolio
                    closed_position = self.portfolio.close_position(token_address, current_price, reason)
                    
                    # Add portfolio summary to exit message
                    exit_message = f"{exit_message}\n\n{self.portfolio.format_portfolio_message()}"
                    
                    # Send to Discord
                    if self.discord_webhook_url:
                        webhook = DiscordWebhook(
                            url=self.discord_webhook_url,
                            content=exit_message
                        )
                        await asyncio.to_thread(webhook.execute)
                    
                    # Print to console
                    console_logger.info("\n" + exit_message + "\n")
                    
                    positions_to_remove.append(token_address)
                    
            except Exception as e:
                console_logger.error(f"Error updating position {position.symbol}: {str(e)}")
                continue
                
        # Remove closed positions
        for token_address in positions_to_remove:
            if token_address in self.portfolio.active_positions:
                del self.portfolio.active_positions[token_address]

    async def start(self):
        """Start the opportunity finder"""
        try:
            self.setup_signal_handlers()
            logger.info("Starting Trade Opportunity Finder...")
            
            # Verify Discord webhook
            if not self.discord_webhook_url:
                raise ValueError("Discord webhook URL not configured!")
            
            # Verify Birdeye API key if using Birdeye
            birdeye_key = os.getenv('BIRDEYE_API_KEY')
            if not birdeye_key or birdeye_key == 'your_birdeye_api_key_here':
                logger.warning("Birdeye API key not configured. Will skip Birdeye data source.")
            
            # Start monitoring
            await self.monitor_opportunities()
            
        except Exception as e:
            logger.error(f"Error starting Trade Opportunity Finder: {e}")
            self.is_running = False
        finally:
            if self.is_running:
                self.is_running = False
                logger.info("Shutting down...")
            
    def analyze_price_pattern(self, changes: Dict) -> Tuple[float, bool]:
        """Analyze price pattern for accumulation signs"""
        try:
            # Safely convert all values to float
            m5 = float(changes.get('m5', '0'))
            m15 = float(changes.get('m15', '0'))
            m30 = float(changes.get('m30', '0'))
            h1 = float(changes.get('h1', '0'))
            h6 = float(changes.get('h6', '0'))
            h24 = float(changes.get('h24', '0'))
            
            # Check for dump pattern (avoid these)
            if any(v < float('-inf') for v in [m5, m15, h1, h24]):
                return 0.0, False
                
            if m5 < -10 or m15 < -15 or h1 < -20:  # Sharp recent drops
                return 0.0, False
                
            if h24 < -30:  # Already dumped in last 24h
                return 0.0, False
                
            # Pattern 1: Slight uptrend with controlled moves
            if all(isinstance(v, (int, float)) for v in [m5, m15, h1]):
                if 0 <= m5 <= 5 and 0 <= m15 <= 8 and -5 <= h1 <= 15:
                    return 0.8, True
                
            # Pattern 2: Recovery from earlier drop
            if all(isinstance(v, (int, float)) for v in [m5, m15, h6, h24]):
                if m5 > 0 and m15 > 0 and h6 < 0 and h24 < 0:
                    return 0.7, True
                
            # Pattern 3: Sideways with increasing volume
            if all(isinstance(v, (int, float)) for v in [m30, h1, h6]):
                if abs(m30) <= 5 and abs(h1) <= 10 and abs(h6) <= 15:
                    return 0.6, True
                
            # Pattern 4: Early stage uptrend
            if all(isinstance(v, (int, float)) for v in [m5, m15, m30, h1]):
                if 0 < m5 < m15 and m15 < m30 and m30 < h1:
                    return 0.9, True
                
            return 0.2, True  # No clear pattern
            
        except (TypeError, ValueError) as e:
            logger.error(f"Error in analyze_price_pattern: {e}")
            return 0.0, False

    def analyze_volume_pattern(self, pair: Dict) -> Tuple[float, float]:
        """Analyze volume pattern for accumulation"""
        try:
            # Get volume data and ensure all values are float
            volume = pair.get('volume', {})
            v_5m = float(volume.get('m5', '0'))
            v_15m = float(volume.get('m15', '0'))
            v_1h = float(volume.get('h1', '0'))
            v_6h = float(volume.get('h6', '0'))
            
            # Get transaction data for different timeframes
            txns = pair.get('txns', {})
            txns_1h = txns.get('h1', {})
            txns_6h = txns.get('h6', {})
            txns_24h = txns.get('h24', {})
            
            # Calculate buy ratios for different timeframes
            def get_buy_ratio(txn_data):
                if not isinstance(txn_data, dict):
                    return 0
                try:
                    buys = float(txn_data.get('buys', '0'))
                    sells = float(txn_data.get('sells', '0'))
                    total = buys + sells
                    return buys / total if total > 0 else 0
                except (ValueError, TypeError):
                    return 0
                
            buy_ratio_1h = get_buy_ratio(txns_1h)
            buy_ratio_6h = get_buy_ratio(txns_6h)
            buy_ratio_24h = get_buy_ratio(txns_24h)
            
            # Volume increase ratio (protect against division by zero)
            vol_change_5m = (v_5m / (v_15m/3)) if v_15m > 0 else 0
            vol_change_15m = (v_15m / (v_1h/4)) if v_1h > 0 else 0
            vol_change_1h = (v_1h / (v_6h/6)) if v_6h > 0 else 0
            
            # Look for healthy volume patterns
            volume_score = 0.0
            
            # Pattern 1: Gradually increasing volume
            if all(v > 0 for v in [vol_change_5m, vol_change_15m, vol_change_1h]):
                if vol_change_5m >= 1.1 and vol_change_15m >= 1.0 and vol_change_1h >= 0.8:
                    volume_score += 0.4
                
            # Pattern 2: Strong recent volume with history
            if all(v > 0 for v in [v_5m, v_15m, v_1h, v_6h]):
                volume_score += 0.3
                
            # Pattern 3: Buy pressure increasing
            if all(v >= 0 for v in [buy_ratio_1h, buy_ratio_6h, buy_ratio_24h]):
                if buy_ratio_1h > buy_ratio_6h and buy_ratio_6h > buy_ratio_24h:
                    volume_score += 0.3
                
            # Avoid suspicious patterns
            if buy_ratio_1h > 0.9:  # Too many buys (potential manipulation)
                volume_score *= 0.5
                
            if v_5m > v_1h * 0.5 and v_1h > 0:  # Volume spike (might be too late)
                volume_score *= 0.4
                
            return volume_score, buy_ratio_1h
            
        except (TypeError, ValueError) as e:
            logger.error(f"Error in analyze_volume_pattern: {e}")
            return 0.0, 0.0

    async def get_top_pairs(self) -> List[Dict]:
        """Get tokens showing early pump signals"""
        async with aiohttp.ClientSession() as session:
            # Focus on search queries that work well
            search_strategies = [
                # Classic Meme Token Patterns
                "https://api.dexscreener.com/latest/dex/search?q=baby+sol",
                "https://api.dexscreener.com/latest/dex/search?q=mini+sol",
                "https://api.dexscreener.com/latest/dex/search?q=moon+sol",
                "https://api.dexscreener.com/latest/dex/search?q=elon+sol",
                "https://api.dexscreener.com/latest/dex/search?q=inu+sol",
                "https://api.dexscreener.com/latest/dex/search?q=pepe+sol",
                "https://api.dexscreener.com/latest/dex/search?q=doge+sol",
                
                # Additional Meme Keywords
                "https://api.dexscreener.com/latest/dex/search?q=shit+sol",
                "https://api.dexscreener.com/latest/dex/search?q=chad+sol",
                "https://api.dexscreener.com/latest/dex/search?q=wojak+sol",
                "https://api.dexscreener.com/latest/dex/search?q=meme+sol",
                "https://api.dexscreener.com/latest/dex/search?q=ai+sol",
                "https://api.dexscreener.com/latest/dex/search?q=gpt+sol",
                
                # New Trending Keywords
                "https://api.dexscreener.com/latest/dex/search?q=based+sol",
                "https://api.dexscreener.com/latest/dex/search?q=bonk+sol",
                "https://api.dexscreener.com/latest/dex/search?q=wojak+sol",
                "https://api.dexscreener.com/latest/dex/search?q=pepo+sol",
                "https://api.dexscreener.com/latest/dex/search?q=cope+sol",
                "https://api.dexscreener.com/latest/dex/search?q=frog+sol",
                
                # Animal-themed
                "https://api.dexscreener.com/latest/dex/search?q=cat+sol",
                "https://api.dexscreener.com/latest/dex/search?q=dog+sol",
                "https://api.dexscreener.com/latest/dex/search?q=ape+sol",
                "https://api.dexscreener.com/latest/dex/search?q=monkey+sol",
                "https://api.dexscreener.com/latest/dex/search?q=bird+sol",
                
                # Popular Culture
                "https://api.dexscreener.com/latest/dex/search?q=trump+sol",
                "https://api.dexscreener.com/latest/dex/search?q=biden+sol",
                "https://api.dexscreener.com/latest/dex/search?q=x+sol",
                "https://api.dexscreener.com/latest/dex/search?q=meta+sol",
                
                # Crypto Slang
                "https://api.dexscreener.com/latest/dex/search?q=fomo+sol",
                "https://api.dexscreener.com/latest/dex/search?q=hodl+sol",
                "https://api.dexscreener.com/latest/dex/search?q=pump+sol",
                "https://api.dexscreener.com/latest/dex/search?q=wagmi+sol",
                "https://api.dexscreener.com/latest/dex/search?q=ngmi+sol",
                "https://api.dexscreener.com/latest/dex/search?q=lfg+sol",
                
                # Token Variations
                "https://api.dexscreener.com/latest/dex/search?q=safe+sol",
                "https://api.dexscreener.com/latest/dex/search?q=fair+sol",
                "https://api.dexscreener.com/latest/dex/search?q=rocket+sol",
                "https://api.dexscreener.com/latest/dex/search?q=lambo+sol",
                "https://api.dexscreener.com/latest/dex/search?q=gem+sol",
                
                # Prefix/Suffix Patterns
                "https://api.dexscreener.com/latest/dex/search?q=sol+2.0",
                "https://api.dexscreener.com/latest/dex/search?q=sol+v2",
                "https://api.dexscreener.com/latest/dex/search?q=sol+pro",
                "https://api.dexscreener.com/latest/dex/search?q=sol+ai",
                
                # Seasonal/Trending
                "https://api.dexscreener.com/latest/dex/search?q=christmas+sol",
                "https://api.dexscreener.com/latest/dex/search?q=santa+sol",
                "https://api.dexscreener.com/latest/dex/search?q=new+year+sol",
                "https://api.dexscreener.com/latest/dex/search?q=valentine+sol",
            ]
            
            all_pairs = []
            seen_pairs = set()
            
            # Process each URL sequentially to avoid rate limits
            for url in search_strategies:
                try:
                    data = await self._rate_limited_call(session, url)
                    if not data or 'pairs' not in data:
                        continue
                        
                    pairs = data['pairs']
                    if not pairs:
                        continue
                        
                    # Process pairs
                    for pair in pairs:
                        if not isinstance(pair, dict):
                            continue
                            
                        if pair.get('chainId') != 'solana':
                            continue
                            
                        pair_address = pair.get('pairAddress')
                        if not pair_address or pair_address in seen_pairs:
                            continue
                        
                        try:
                            # Basic metrics
                            liquidity = float(pair.get('liquidity', {}).get('usd', '0'))
                            volume_24h = float(pair.get('volume', {}).get('h24', '0'))
                            created_at = float(pair.get('pairCreatedAt', '0'))
                            price = float(pair.get('priceUsd', '0'))
                            
                            # Skip if too old (> 7 days)
                            if time.time() - created_at > 7 * 24 * 3600:
                                continue
                                
                            # Skip if liquidity too high/low
                            if liquidity > 30000 or liquidity < 500:
                                continue
                                
                            # Check if it's pumping
                            is_pump, pump_score, reason = self.is_pumping(pair)
                            if not is_pump:
                                continue
                                
                            # Store analysis
                            pair['_analysis'] = {
                                'pump_score': pump_score,
                                'reason': reason
                            }
                            
                            # Log found pair
                            symbol = pair.get('baseToken', {}).get('symbol', 'Unknown')
                            console_logger.info(f"🎯 Found potential pump: {symbol} at ${price:.8f}")
                            
                            # Add to collection
                            seen_pairs.add(pair_address)
                            all_pairs.append(pair)
                            
                        except (TypeError, ValueError) as e:
                            logger.error(f"Error processing pair: {str(e)}")
                            continue
                            
                except Exception as e:
                    console_logger.error(f"Error processing URL {url}: {str(e)}")
                    continue
            
            if not all_pairs:
                console_logger.warning("No pumping pairs found in this scan")
                return []
            
            # Sort by pump score
            all_pairs.sort(key=lambda x: x.get('_analysis', {}).get('pump_score', 0), reverse=True)
            
            # Take top pumping targets
            final_pairs = all_pairs[:10]  # Focus on clearest signals
            
            if final_pairs:
                console_logger.info(f"✨ Found {len(final_pairs)} tokens showing pump signals")
                
                # Show summary of findings
                console_logger.info("\n🏆 Top Candidates:")
                for i, pair in enumerate(final_pairs, 1):
                    try:
                        symbol = pair.get('baseToken', {}).get('symbol', 'Unknown')
                        price = float(pair.get('priceUsd', '0'))
                        score = float(pair.get('_analysis', {}).get('pump_score', '0'))
                        reason = pair.get('_analysis', {}).get('reason', '')
                        console_logger.info(f"{i}. {symbol} - ${price:.8f} (Score: {score:.2f}) - {reason}")
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error formatting pair data: {str(e)}")
                        continue
                console_logger.info("")
            
            return final_pairs

    async def monitor_opportunities(self):
        """Monitor for trading opportunities"""
        self.is_running = True
        last_check = {}  # Store last check time for each token
        
        console_logger.info("\n🚀 Starting Meme Token Watcher...")
        console_logger.info("Press Ctrl+C to stop\n")
        
        while self.is_running:
            try:
                console_logger.info("🔍 Scanning for opportunities...")
                pairs = await self.get_top_pairs()
                
                if not pairs:
                    console_logger.info("⚠️  No pairs found, retrying in 60s...")
                    await asyncio.sleep(self.monitoring_interval)
                    continue
                
                console_logger.info(f"📊 Analyzing {len(pairs)} tokens...")
                
                for pair in pairs:
                    try:
                        token_address = pair.get('baseToken', {}).get('address')
                        if not token_address:
                            continue
                            
                        current_time = time.time()
                        if token_address in last_check:
                            time_since_last = current_time - last_check[token_address]
                            if time_since_last < self.alert_cooldown:
                                continue
                                
                        # Check if it's pumping
                        is_pump, pump_score, reason = self.is_pumping(pair)
                        if not is_pump:
                            continue
                            
                        # Extract and convert metrics
                        try:
                            symbol = pair.get('baseToken', {}).get('symbol', 'Unknown')
                            price = float(pair.get('priceUsd', '0'))
                            liquidity = float(pair.get('liquidity', {}).get('usd', '0'))
                            h1_change = float(pair.get('priceChange', {}).get('h1', '0'))
                            
                            # Print signal to console
                            console_logger.info(f"\n🎯 Signal Detected: {symbol}")
                            console_logger.info(f"💰 Price: ${price:.8f}")
                            console_logger.info(f"📈 1h Change: {h1_change:+.2f}%")
                            console_logger.info(f"💧 Liquidity: ${liquidity:,.0f}")
                            console_logger.info(f"⚠️  Risk Score: {pump_score:.2f}/1.0")
                            
                            # Add analysis data to pair
                            pair['_analysis'] = {
                                'pump_score': pump_score,
                                'reason': reason
                            }
                            
                            # Send Discord alert
                            if self.should_send_alert(token_address, symbol):
                                await self.send_discord_alert(pair)
                                last_check[token_address] = current_time
                                
                        except (ValueError, TypeError) as e:
                            console_logger.error(f"Error processing metrics for {symbol}: {e}")
                            continue
                            
                    except Exception as e:
                        console_logger.error(f"Error analyzing pair: {e}")
                        continue
                    
                console_logger.info("\n⏳ Waiting for next scan...")
                await asyncio.sleep(self.monitoring_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                console_logger.error(f"❌ Error: {str(e)}")
                await asyncio.sleep(self.monitoring_interval)

    async def start(self):
        """Start the opportunity finder"""
        try:
            self.setup_signal_handlers()
            logger.info("Starting Trade Opportunity Finder...")
            
            # Verify Discord webhook
            if not self.discord_webhook_url:
                raise ValueError("Discord webhook URL not configured!")
            
            # Verify Birdeye API key if using Birdeye
            birdeye_key = os.getenv('BIRDEYE_API_KEY')
            if not birdeye_key or birdeye_key == 'your_birdeye_api_key_here':
                logger.warning("Birdeye API key not configured. Will skip Birdeye data source.")
            
            # Start monitoring
            await self.monitor_opportunities()
            
        except Exception as e:
            logger.error(f"Error starting Trade Opportunity Finder: {e}")
            self.is_running = False
        finally:
            if self.is_running:
                self.is_running = False
                logger.info("Shutting down...")
            
    def is_pumping(self, pair: Dict) -> Tuple[bool, float, str]:
        """Check if a token is in early pump phase"""
        try:
            # Extract and validate basic metrics
            try:
                # Get price changes
                changes = pair.get('priceChange', {})
                m5 = float(changes.get('m5', '0'))
                m15 = float(changes.get('m15', '0'))
                h1 = float(changes.get('h1', '0'))
                h24 = float(changes.get('h24', '0'))
                
                # Get volume data
                volume = pair.get('volume', {})
                v_5m = float(volume.get('m5', '0'))
                v_1h = float(volume.get('h1', '0'))
                
                # Get liquidity
                liquidity = float(pair.get('liquidity', {}).get('usd', '0'))
                
                # Get transaction data
                txns = pair.get('txns', {}).get('h1', {})
                buys = int(txns.get('buys', '0'))
                sells = int(txns.get('sells', '0'))
                
            except (ValueError, TypeError) as e:
                logger.error(f"Error converting metrics in is_pumping: {e}")
                return False, 0.0, "Error in metrics"
            
            # Quick rejection checks
            if h24 < -20:  # Already dumped
                return False, 0.0, "Already dumped"
                
            if buys + sells < 3:  # Too inactive
                return False, 0.0, "Too inactive"
                
            if liquidity < 500 or liquidity > 50000:  # Liquidity outside range
                return False, 0.0, "Invalid liquidity"
            
            # Initialize score components
            score = 0.0
            reasons = []
            
            # Check for sharp initial price movement
            if m5 >= 5 and m15 >= 10:
                score += 0.3
                reasons.append("Sharp price movement")
            
            # Check for building momentum
            if m5 > 0 and m15 > m5 and h1 > m15:
                score += 0.2
                reasons.append("Building momentum")
            
            # Check for volume spike
            if v_5m > (v_1h / 12) * 2:  # 5m volume > 2x average 5m volume
                score += 0.25
                reasons.append("Volume spike")
            
            # Check buy pressure
            if buys > sells * 1.5:  # 50% more buys than sells
                score += 0.25
                reasons.append("Strong buy pressure")
            
            # Determine if it's pumping
            is_pumping = score >= 0.5
            reason = " + ".join(reasons) if reasons else "Multiple indicators"
            
            return is_pumping, score, reason
            
        except Exception as e:
            logger.error(f"Error in is_pumping: {e}")
            return False, 0.0, "Error analyzing"

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
        asyncio.run(finder.monitor_opportunities())
    except KeyboardInterrupt:
        console_logger.info("\n👋 Shutting down gracefully...")
    except Exception as e:
        console_logger.error(f"❌ Error: {str(e)}")
        raise
