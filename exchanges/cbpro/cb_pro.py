# '''
#  OldMonk Auto trading Bot
#  Desc: CBPRO exchange interactions
#  (c) 2017-2019 Joshith Rayaroth Koderi
# '''

# import requests
import json
import pprint
from decimal import Decimal
from datetime import datetime, timedelta
from time import sleep
import time
from dateutil.tz import tzlocal
from threading import Thread
import threading

# import gdax as CBPRO #Official version seems to be old, doesn't support auth websocket client
import cbpro

# from pstats import add_callers
from utils import getLogger, readConf
from market import Market, OHLC, feed_enQ, get_market_by_product, Order
from exchanges import Exchange

EXHANGE_NAME = "CBPRO"
log = getLogger (EXHANGE_NAME)
log.setLevel(log.DEBUG)


#CBPRO CONFIG FILE
CBPRO_CONF = 'config/cbpro.yml'

class CBPRO (Exchange):
    name = EXHANGE_NAME
    gdax_conf = {}
    gdax_products = []
    gdax_accounts = {}
    public_client = None
    auth_client   = None
    ws_client = None
    primary = False
    candle_interval = 0
    def __init__(self, config=CBPRO_CONF, primary=False):
        log.info('init CBPRO exchange')        
        
        conf = readConf (config)
        if (conf != None and len(conf)):
            self.gdax_conf = conf['exchange']
        else:
            return None
        
        self.primary = True if primary else False
        #get config
        backfill = self.gdax_conf.get('backfill')
        if not backfill:
            log.fatal("Invalid Config file")            
            return None
    
        for entry in backfill:
            if entry.get('enabled'):
                self.gdax_conf['backfill_enabled'] = entry['enabled']
            if entry.get('period'):
                self.gdax_conf['backfill_period'] = int(entry['period'])
            if entry.get('interval'):
                self.gdax_conf['backfill_interval'] = int(entry['interval'])            
        
        self.key = self.gdax_conf.get('apiKey')
        self.b64secret = self.gdax_conf.get('apiSecret')
        self.passphrase = self.gdax_conf.get('apiPassphrase')
        self.api_base = self.gdax_conf.get ('apiBase')
        self.feed_base = self.gdax_conf.get ('wsFeed')
        
#         self.max_fund_liquidity_percent = self.gdax_conf.get ('fundMaxLiquidity')
                
        self.public_client = cbpro.PublicClient(api_url=self.api_base)
        if (self.public_client) == None :
            log.critical("gdax public client init failed")
            return None
                        
        if ((self.key and self.b64secret and self.passphrase and self.api_base ) == False):
            log.critical ("Invalid API Credentials in cbpro Config!! ")
            return None
        
        self.auth_client = cbpro.AuthenticatedClient(self.key, self.b64secret, self.passphrase,
                                      api_url=self.api_base)
        
        if self.auth_client == None:
            log.critical("Unable to Authenticate with cbpro exchange. Abort!!")
            return None
            
        #time sync
        serverTime = int(self.public_client.get_time()['epoch'])*1000
        
        localTime = time.time()*1000
        self.timeOffset = (serverTime - localTime)//1000
        if abs(self.timeOffset) < 5:
            self.timeOffset = 0        
        log.info ("servertime: %d localtime: %d offset: %d"%(serverTime, localTime, self.timeOffset))
        
#         global gdax_products
        products = self.public_client.get_products()
        if (len(products) and len (self.gdax_conf['products'])):
            for prod in products:
                for p in self.gdax_conf['products']:              
                    if prod['id'] in p.keys():
#                         prod['max_per_buy_fund_val'] = p[prod['id']].get ('fundMaxPerBuyValue', 0)
#                         prod['max_per_trade_asset_size'] = p[prod['id']].get ('assetMaxPerTradeSize', 0)
#                         prod['min_per_trade_asset_size'] = p[prod['id']].get ('assetMinPerTradeSize', 0)        
#                         if (prod['max_per_buy_fund_val'] == 0 or prod['max_per_trade_asset_size'] == 0 or 
#                            prod['min_per_trade_asset_size'] == 0 ):
#                             log.critical ("invalid config for product: %s"%(prod['id']))
#                             raise Exception ("invalid config for product: %s"%(prod['id']))            
#                             return False       
                        self.gdax_products.append(prod)
        
        # Popoulate the account details for each interested currencies
        accounts =  self.auth_client.get_accounts()
        if (accounts == None):
            log.critical("Unable to get account details!!")
            raise Exception ("Unable to get CBPRO Accounts!!")  
            return False
        
        log.info ("Exchange Accounts: %s"%(pprint.pformat(accounts, 4)))
        if isinstance(accounts, dict):
                    ## Error Case
                    #{   u'message': u'Forbidden'}                    
                    err_msg = accounts.get('message')        
                    if (err_msg == 'Forbidden'):
                        log.critical("Forbidden to get accounts. Potential permissions issue")
                        raise Exception ("Unable to get CBPRO Accounts!! Potential permissions issue.")
                    elif (err_msg != None):
                        log.critical("error while getting accounts: msg: %s"%err_msg)
                        raise Exception ("error while getting accounts: msg: %s"%err_msg)
        for account in accounts:
            for prod in self.gdax_conf['products']:
                for prod_id in prod.keys():
                    currency = prod[prod_id]['currency']
                    if account['currency'] in currency:
                        log.debug ("Interested Account Found for Currency: "+account['currency'])
                        self.gdax_accounts[account['currency']] = account
                        break

        self.start_wsfeed ()

        self.candle_interval = int(self.gdax_conf.get('backfill_interval'))
        log.info( "**CBPRO init success**\n Products: %s\n Accounts: %s"%(
                        pprint.pformat(self.gdax_products, 4), pprint.pformat(self.gdax_accounts, 4)))
        
    def start_wsfeed(self):
        # register websocket feed 
        self.ws_client = self._register_feed (api_key=self.key, api_secret=self.b64secret,
                                               api_passphrase=self.passphrase, url=self.feed_base)
        if self.ws_client == None:
            log.critical("Unable to get websocket feed. Abort!!")
            return None
        
        #Start websocket Feed Client
        if (self.ws_client != None):
            log.debug ("Starting Websocket Feed... ")
            self.ws_client.start()
     
    def market_init (self, market):
#         global ws_client
        usd_acc = self.gdax_accounts['USD']
        crypto_acc = self.gdax_accounts.get(product['base_currency'])
        if (usd_acc == None or crypto_acc == None): 
            log.error ("No account available for product: %s"%(market.product_id))
            return None
        
#         #Setup the initial params
#         market = Market(product=product, exchange=self)    
        market.fund.set_initial_value(Decimal(usd_acc['available']))
        market.fund.set_hold_value(Decimal(usd_acc['hold']))
#         market.fund.set_fund_liquidity_percent(self.max_fund_liquidity_percent)       
#         market.fund.set_max_per_buy_fund_value(product['max_per_buy_fund_val'])
#         market.fund.set_fee(self.gdax_conf['Fee']['maker'], self.gdax_conf['Fee']['taker'])
        market.asset.set_initial_size(Decimal( crypto_acc['available']))
        market.asset.set_hold_size( Decimal(crypto_acc['hold']))
#         market.asset.set_max_per_trade_size(product['max_per_trade_asset_size'])
#         market.asset.set_min_per_trade_size(product['min_per_trade_asset_size'])        
#         market.asset.set_hold_size(product['asset_hold_size'])
        
        ## Feed Cb
        market.register_feed_processor(self._gdax_consume_feed)
        
        ## Init Exchange specific private state variables
        market.set_candle_interval (self.candle_interval)
        
#         #set whether primary or secondary
#         market.primary = self.primary
                
        return market
        
    def close (self):
        log.debug("Closing exchange...")    
#         global self.ws_client
        if (self.ws_client):
            log.debug("Closing WebSocket Client")
            self.ws_client.close ()        
        log.critical ("exch being closed")

    def add_candle(self, market):
        # close the current candle period and start a new candle period
        candle = OHLC(long(time.time()), market.O, market.H, market.L, market.get_market_rate(), market.V)
        log.info ("New candle identified %s"%(candle))
        market.add_new_candle (candle)   
   
    def _normalized_order (self, order):
        '''
        Desc:
         Error Handle and Normalize the order json returned by gdax
          to return the normalized order detail back to callers
          Handles -
          1. Initial Order Creation/Order Query
          2. Order Update Feed Messages
          Ref: https://docs.gdax.com/#the-code-classprettyprintfullcode-channel
        Sample order:
                {u'created_at': u'2018-01-10T09:49:02.639681Z',
                 u'fill_fees': u'0.0000000000000000',
                 u'filled_size': u'0.00000000',
                 u'executed_value': u'29.9998711699000000',                 
                 u'id': u'7150b013-62ca-49c7-aa28-4a9473778644',
                 u'post_only': True,
                 u'price': u'14296.99000000',
                 u'product_id': u'BTC-USD',
                 u'settled': False,
                 u'side': u'buy',
                 u'funds': u'2959.4764175800000000',                 
                 u'size': u'0.13988959',
                 u'status': u'pending',
                 u'stp': u'dc',
                 u'time_in_force': u'GTC',
                 u'type': u'limit'}    
        Known Errors: 
          1. {u'message': u'request timestamp expired'}
          2. {u'message': u'Insufficient funds'}
          3. {'status' : 'rejected', 'reject_reason': 'post-only'}
        '''
        error_status_codes = ['rejected']
        log.debug ("Order msg:\n%s"%(pprint.pformat(order, 4)))
        
        msg = order.get('message')
        status = order.get('status')
        if (msg or (status in error_status_codes)):
            log.error("FAILED Order: error msg: %s status: %s"%(msg, status))
            return None
    
        # Valid Order
        product_id = order.get('product_id')
        order_id   = order.get('id') or order.get('order_id')
        order_type = order.get('type')
        status_reason = order.get('reason') or order.get('done_reason')
        status_type = order.get('status') 
        if order_type in ['received', 'open', 'done', 'match', 'change', 'margin_profile_update', 'activate' ]:
            # order status update message
            status_type = order_type
            order_type = order.get('order_type') #could be None
        else:
            pass
        create_time = order.get('created_at') or None
        update_time  = order.get('time') or order.get('done_at') or None
        side = order.get('side') or None
        # Money matters
        price =   Decimal(order.get('price') or 0)
        request_size  = Decimal(order.get('size') or  0)
        filled_size = Decimal(order.get('filled_size') or 0)
        remaining_size  = Decimal(order.get('remaining_size') or 0)
        funds = Decimal(order.get('executed_value') or order.get('funds') or order.get('specified_funds') or 0)
        fees = Decimal(order.get('fees') or order.get('fill_fees') or 0)
        if order.get('settled') == True:
            total_val = Decimal(order.get('executed_value') or 0)
            if total_val and filled_size and not price:
                price = total_val/filled_size
            if (funds == 0):
                funds = total_val + fees
                #log.debug ("calculated fill price: %g size: %g"%(price, filled_size))
    #         if filled_size and remaining_size:
    #             request_size = filled_size + remaining_size
                        
        if (request_size == 0):
            request_size = remaining_size + filled_size  
            
        log.debug ("price: %g fund: %g req_size: %g filled_size: %g remaining_size: %g fees: %g"%(
            price, funds, request_size, filled_size, remaining_size, fees))
        norm_order = Order (order_id, product_id, status_type, order_type=order_type, status_reason=status_reason,
                            side=side, request_size=request_size, filled_size=filled_size, remaining_size=remaining_size,
                             price=price, funds=funds, fees=fees, create_time=create_time, update_time=update_time)
        return norm_order
    
    ######### WebSocket Client implementation #########
    
    def _register_feed (self, api_key="", api_secret="", api_passphrase="", url=""):
        products = []
        for p in self.gdax_conf['products']: #["BTC-USD", "ETH-USD"]
            products += p.keys()
            
        channels = [
#                 "level2",
                "heartbeat",
                "ticker",
                "user"         #Receive details about our orders only
            ]
        message_type = "subscribe"
        websocket_client = cbproWebsocketClient (url, products=products, message_type=message_type,
                                                should_print=False, auth=True,
                                                api_key=api_key, api_secret=api_secret,
                                                 api_passphrase=api_passphrase, channels=channels)
        if websocket_client == None:
            log.error ("Unable to register websocket client")
            return None
        else:
            log.debug ("Initialized websocket client for products: %s"%(products))        
            return websocket_client
    
    ######## Feed Consume #######
    def _gdax_consume_feed (self, market, msg):
        ''' 
        Feed Call back for Gdax    
        This is where we do all the useful stuff with Feed
        '''
        msg_type = msg['type']
        #log.debug ("Feed received: msg:\n %s"%(json.dumps(msg, indent=4, sort_keys=True)))
        if (msg_type == 'ticker'):
            self._gdax_consume_ticker_feed (market, msg)
        elif (msg_type == 'heartbeat'):
            log.debug ("Feed: Heartbeat")
        elif (msg_type == 'snapshot'):
            self._gdax_consume_l2_book_snapshot (market, msg)
        elif (msg_type == 'l2update'):
            self._gdax_consume_l2_book_update (market, msg)        
        elif (msg_type in ['pending', 'received' , 'open' , 'done' , 'change'] ):
            self._gdax_consume_order_update_feed (market, msg)
        elif (msg_type == 'error'):
            log.error ("Feed: Error Msg received on Feed msg: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
        else:
            log.error ("Feed: Unknown Feed Msg Type (%s) msg: %s"%(msg['type'], json.dumps(msg, indent=4, sort_keys=True)))
    
    def _gdax_consume_order_update_feed (self, market, msg):
        ''' 
        Process the order status update feed msg 
        '''
        log.debug ("Order Status Update id:%s"%(msg.get('order_id')))
        order = self._normalized_order(msg)
        market.order_status_update (order)
    
        
    def _gdax_consume_l2_book_snapshot (self, market, msg):
        '''
         Consume the OrderBook Snapshot and build orderbook
        '''    
        bids = msg ['bids'] or []
        asks = msg ['asks'] or []
        log.debug ("Building orderbook from snapshot for (%s): num_bids(%d) num_asks (%d) "%(
            market.name, len(bids), len(asks)))
        market.order_book.new_book (bids, asks)
        
    def _gdax_consume_l2_book_update (self, market, msg):
        '''
        {
            "type": "l2update",
            "product_id": "BTC-EUR",
            "changes": [
                ['side', 'price', 'size']
                ["buy", "1", "3"],
                ["sell", "3", "1"],
                ["sell", "2", "2"],
                ["sell", "4", "0"]
            ]
        }
        '''
        log.debug ("Updating L2 order book")
        changes = msg['changes'] or []
        for change in changes:
            side = change[0]
            price = change [1]
            size = change[2]
            if (side == 'buy'):
                market.order_book.add_bids([[price, size]])
            elif (size == 'sell'):
                market.order_book.add_asks([[price, size]])            
            
    def _gdax_consume_ticker_feed (self, market, msg):
#         '''
#         {
#             "best_ask": "11367.87", 
#             "best_bid": "11366.44", 
#             "high_24h": "12019.44000000", 
#             "last_size": "0.01000000", 
#             "low_24h": "11367.87000000", 
#             "open_24h": "10896.04000000", 
#             "price": "11367.87000000", 
#             "product_id": "BTC-USD", 
#             "sequence": 3000902, 
#             "side": "buy", 
#             "time": "2018-01-19T09:53:02.816000Z", 
#             "trade_id": 566669, 
#             "type": "ticker", 
#             "volume_24h": "264992.94231824", 
#             "volume_30d": "364800.84143217"
#         }
#         '''
#         global self.gdax_conf
#         log.debug ("Ticker Feed:%s"%(json.dumps(msg, indent=4, sort_keys=True)))
        
        #log.debug ("consuming ticker feed")
        price = Decimal(msg.get('price'))
        last_size = msg.get('last_size')
        if (price == 0 or not last_size):
            log.error ("Invalid price or 'last_size' in ticker feed")
            return
        last_size = Decimal(last_size)
        o = market.O
        h = market.H
        l = market.L
        v = market.V
        
        #update ticker
        if o == 0:
            market.O = market.H = market.L = price
        else:
            if price < l:
                market.L = price
            if price > h:
                market.H = price
        v += last_size
        market.V = v
        
        now = time.time()
#         log.critical("next_cdl: %d now: %d"%(market.cur_candle_time+self.candle_interval, now))
        if now >= market.cur_candle_time + self.candle_interval:
            # close the current candle period and start a new candle period
            c = price
            candle = OHLC(long(now), market.O, market.H, market.L, c, market.V)
            log.debug ("New candle identified %s"%(candle))        
            market.add_new_candle (candle)
            
            
        #TODO: FIXME: jork: might need to rate-limit the logic here after
        market.set_market_rate (price)
#         market.update_market_states()
    
        
    ###################### WebSocket Impl : end #############################
        
    ############ ************** Public APIs for Exchange *********** ###########    
    
    # def products():
    #     api_base = gdax_conf['apiBase']
    #     response = requests.get(api_base + '/products')
    #     # check for invalid api response
    #     if response.status_code is not 200:
    #         raise Exception('Invalid CBPRO Status Code: %d' % response.status_code)
    #     #log.debug(response.json())
    #     return response.json()
    
    def get_products(self):
        """
        Get registered products on this exchange
        """
        #  log.debug(gdax_products)    
        return self.gdax_products
    
    def get_product_order_book (self, product, level = 1):
        '''
        Get the order book at specified level
        '''
        v = self.public_client.get_product_order_book(product, level)
        #log.debug(v)
        return v
    
    def get_accounts (self):
    #     log.debug (pprint.pformat(self.gdax_accounts))
        return self.gdax_accounts    
    
    
    def get_historic_rates (self, product_id, start=None, end=None):
        '''
            Args:
        product_id (str): Product
        start (Optional[str]): Start time in ISO 8601
        end (Optional[str]): End time in ISO 8601
        interval (Optional[str]): Desired time slice in 
         seconds
         '''
        #Max Candles in one call
        
        max_candles = 200
        candles_list = []
        
        #get config
        enabled = self.gdax_conf.get('backfill_enabled')
        period = int(self.gdax_conf.get('backfill_period'))
        interval = self.candle_interval 
        
        if not enabled:
            log.debug ("Historical data retrieval not enabled")
            return None
    
        if not end:
            # if no end, use current time
            end = datetime.now()
             
        if not start:
            # if no start given, use the config
            real_start = start = end - timedelta(days = period)
        else:
            real_start = start
        
        log.debug ("Retrieving Historic candles for period: %s to %s"%(
                    real_start.isoformat(), end.isoformat()))
        
        td = max_candles*interval
        tmp_end = start + timedelta(seconds = td)
        if tmp_end > end:
            tmp_end = end
        
        #adjust with serverTime
        start = start + timedelta(seconds = self.timeOffset)
        tmp_end = tmp_end + timedelta(seconds = self.timeOffset)
        
        count = 0
        while (start < end):
            ## looks like there is a rate=limiting in force on gdax, we will have to slow down
            count += 1
            if (count >= 2):
                #rate-limiting
                count = 0
                sleep (2)
            
            start_str = start.replace(tzinfo=tzlocal()).isoformat()
            end_str = tmp_end.replace(tzinfo=tzlocal()).isoformat()
            candles = self.public_client.get_product_historic_rates (product_id, start_str, end_str, interval)
            if candles:
                if isinstance(candles, dict):
                    ## Error Case
                    err_msg = candles.get('message')
                    if (err_msg):
                        log.error ("Error while retrieving Historic rates: msg: %s\n will retry.."%(err_msg))
                else:
                    #candles are of struct [[time, o, h, l,c, V]]
                    candles_list += map(
                        lambda candle: OHLC(time=candle[0], 
                                            low=candle[1], high=candle[2], open=candle[3], 
                                            close=candle[4], volume=candle[5]), reversed(candles))
    #                 log.debug ("%s"%(candles))
                    log.debug ("Historic candles for period: %s to %s num_candles: %d "%(
                        start_str, end_str, (0 if not candles else len(candles))))
                    
                    # new period, start from the (last +1)th position
                    start = tmp_end + timedelta(seconds = interval)
                    tmp_end = start + timedelta(seconds = td)
                    if tmp_end > end:
                        tmp_end = end
            else:
                log.error ("Error While Retrieving Historic candles for period: %s to %s num: %d"%(
                    start_str, end_str, (0 if not candles else len(candles))))
                return None
        
        log.debug ("Retrieved Historic candles for period: %s to %s num: %d"%(
                    real_start.isoformat(), end.isoformat(), (0 if not candles_list else len(candles_list))))
    #     log.debug ("%s"%(candles_list))
        return candles_list
        
    
    def buy (self, trade_req) :
        #TODO: FIXME: Implement Market/STOP orders
        log.debug ("BUY - Placing Order on exchange --" )
        order = self.auth_client.buy(product_id=trade_req.product, order_type=trade_req.type,
                        price=trade_req.price, #USD
                        size=trade_req.size, #BTC
                        post_only='True'                    
                        )
        return self._normalized_order (order);
    
    def sell (self, trade_req) :
        #TODO: FIXME: Implement Market/STOP orders        
        log.debug ("SELL - Placing Order on exchange --" )
        order = self.auth_client.sell(product_id=trade_req.product, order_type=trade_req.type,
                        price=trade_req.price, #USD
                        size=trade_req.size, #BTC
                        post_only='True'
                        )
        return self._normalized_order (order);
    
    def get_order (self,  order_id):
        log.debug ("GET - order (%s) "%(order_id))
        order = self.auth_client.get_order(order_id)
        return self._normalized_order (order);
    
    def cancel_order (self, order_id):
        log.debug ("CANCEL - order (%s) "%(order_id))
        self.auth_client.cancel_order(order_id)
        


class cbproWebsocketClient (cbpro.WebsocketClient):
#     __init__(self, url="wss://ws-feed.gdax.com", products=None, message_type="subscribe", mongo_collection=None,
#                  should_print=True, auth=False, api_key="", api_secret="", api_passphrase="", channels=None):
        name = EXHANGE_NAME
        def start(self):
            log.info ("starting cbproWebsocketClient")
            
            def _go():
                self._connect()
                self._listen()
                self._disconnect()
    
            self.stop = False
            self.ws_error = False            
            self.on_open()
            self.thread = Thread(target=_go)
            self.thread.start()        
            
            self.hearbeat_time = time.time()
            self.keepalive_thread = Thread(target=self._keepalive)
            self.keepalive_thread.start()
        
        def restart (self):
            self.thread.join()
            log.info ("restarting cbproWebsocketClient")
            def _go():
                self._connect()
                self._listen()
                self._disconnect()
    
            self.stop = False          
            self.ws_error = False            
            self.on_open()
            self.thread = Thread(target=_go)
            self.thread.start()
        def close(self):
            log.info ("closing ws and keep alive threads")
            self.stop = True
            self.thread.join()
            log.debug ("waiting to close alive threads")            
            self.keepalive_thread.join()
            log.debug ("closed ws and keep alive threads")     
                    
        def _keepalive(self, interval=10):
            while not self.stop :
                #TODO: FIXME: potential race
                if self.hearbeat_time + 30 < (time.time()):
                    #heartbeat failed
                    log.error ("Heartbeat failed!! last_hb_time: %d cur_time: %d \
                    potential websocket death, restarting"%(self.hearbeat_time, time.time()))
                    if (self.stop):
                        log.info ("websocket attempt close intentionally")
                        break
                    log.debug ("before ws restart. active thread count: %d"% threading.active_count())                     
                    self.restart()
                    log.debug ("after ws restart. active thread count: %d"% threading.active_count())                                         
                    
                time.sleep(interval)            
            
        def on_open(self):
            #self.url = "wss://ws-feed.gdax.com/"
            self.message_count = 0
            print("Let's count the messages!")

        def on_message(self, msg):
            self.feed_enQ_msg (msg)
            #print(json.dumps(msg, indent=4, sort_keys=True))
            self.message_count += 1

        def on_close(self):
            print("\n-- Goodbye! --")
            
        def on_error(self, e, data=None):
            self.error = e
            self.ws_error = True
            log.critical('error: %s - data: %s'%(e, data))
               
        #to fix the connection drop bug
        def _listen(self):
            self.time = time.time()
            while not self.stop and not self.ws_error:
                try:
                    start_t = 0
                    if time.time() - start_t >= 30:
                        # Set a 30 second ping to keep connection alive
                        self.ws.ping("keepalive")
                        start_t = time.time()
                    data = self.ws.recv()
                    msg = json.loads(data)
                except ValueError as e:
                    self.on_error(e)
                except Exception as e:
                    self.on_error(e)
                else:
                    self.on_message(msg)
#             if (self.stop): 
#                 log.info ("ws listen finished. waiting to finish keepalive thread")
#                 self.keepalive_thread.join()
                            
        def feed_enQ_msg (self, msg):
            #print("Feed MSG: %s"%(json.dumps(msg, indent=4, sort_keys=True)))     
            msg_type = msg.get('type') 
            product_id = msg.get("product_id")
            if (msg_type == 'ticker'):
                if (product_id == None):
                    log.error ("Feed Thread: Invalid Product-id: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return
                market = get_market_by_product (self.name, product_id)
                if (market == None):
                    log.error ("Feed Thread: Unknown market: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return                
                feed_enQ(market, msg)
            elif (msg_type == 'snapshot') or (msg_type == 'l2update'):
                if (product_id == None):
                    log.error ("Feed Thread: Invalid Product-id: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return
                market = get_market_by_product (self.name, product_id)
                if (market == None):
                    log.error ("Feed Thread: Unknown market: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return                
                feed_enQ(market, msg)
            elif (msg_type in [ 'received','open' ,'done', 'change']):    
                if (product_id == None):
                    log.error ("Feed Thread: Invalid Product-id: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return
                market = get_market_by_product (self.name, product_id)
                if (market == None):
                    log.error ("Feed Thread: Unknown market: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
                    return                  
                feed_enQ(market, msg)
            elif (msg_type == 'match'):
                #log.debug ("Feed Thread: Match msg : IGNORED")
                pass
            elif (msg_type == 'heartbeat'):
                #log.debug ("Feed Thread: Heartbeat: IGNORED")
                self.hearbeat_time = time.time()
            elif (msg_type == 'subscriptions'):          
                log.debug ("Feed: Subscribed to WS feed %s"%(json.dumps(msg, indent=4, sort_keys=True)))
            elif (msg_type == 'error'):
                log.error ("Feed Thread: Error Msg received on Feed msg: %s"%(json.dumps(msg, indent=4, sort_keys=True)))
            else:
                log.error ("Feed Thread: Unknown Feed Msg Type (%s)"%(msg['type']))
#EOF    