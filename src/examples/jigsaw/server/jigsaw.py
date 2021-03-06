from collections import defaultdict, deque
from copy import deepcopy
from data.db.sqlalchemyconn import SQLAlchemyConnector
from data.plugins.obm import ObjectManager
from examples.jigsaw.server.mapper.dbobjects import Piece, User
from examples.jigsaw.server.mapper.mapper import JigsawMapper
from multiprocessing import Lock
from multiprocessing.pool import ThreadPool
from random import randint
from rcat import RCAT
from threading import Thread
from tornado import websocket
from tornado.ioloop import IOLoop
import ConfigParser
import common.helper as helper
import functools
import json
import logging.config
import random
import time
import uuid

global db
global pc
global settings
global datacon
global jigsaw
global workers

workers = None
terminal = None
jigsaw = None
settings = {}
db = None
locks = {}
datacon = None
rcat = None
tables = {}
location = {}
pchandler = None
game_loading = True 
coordinator = None
total_pieces = 0
piece_locks = defaultdict(deque)

img_settings = {}
board = {}
grid = {}
qlock = {}

# default frustum; w and h are determined by each client's canvas size
dfrus = {'x': 0,
         'y':0,
         'scale':1,
         'w': None,
         'h': None
         }

pieces = {}

class JigsawServerHandler(websocket.WebSocketHandler):
    def open(self):
        global pchandler
        pchandler = self
        logging.debug("[tornado]: Jigsaw App Websocket Open")


    def on_message(self, message):
        try:
            msg = json.loads(message)
            pid = None
            logging.debug("[jigsaw]: Message: " + message)
            if "M" in msg:
                # Must guarantee ordering for same object ids
                if "pd" in msg["M"]:
                    pid = json.loads(msg["M"])["pd"]["id"]
                elif "pm" in msg["M"]:
                    pid = json.loads(msg["M"])["pm"]["id"]
                    
                if pid:
                    msg["_pid_"] = pid
            if pid:
                if not pid in qlock:
                    qlock[pid] = Lock()
                qlock[pid].acquire()
                if len(piece_locks[pid]) == 0:
                    workers.apply_async(request_parser, [msg], callback=unqueue)
                piece_locks[pid].append(msg)
                qlock[pid].release()
            else:
                workers.apply_async(request_parser, [msg])
        except:
            logging.exception("[tornado]: Couldn't parse the message.")
            
    def on_close(self):
        logging.debug("[tornado]: App WebSocket closed")
    
    def sync_reply(self,message):
        self.write_message(message)
    
handlers = [
    (r"/", JigsawServerHandler)
]

def unqueue(pid):
    # Lock to check if there is more work to be done for this object.
    qlock[pid].acquire()
    try:
        # Remove the current request from the queue
        piece_locks[pid].popleft()
        
        if not len(piece_locks[pid]) == 0:
            newmsg = piece_locks[pid][0] 
            workers.apply_async(request_parser, [newmsg], callback=unqueue)
        
    # If empty, no prob, we are just done.
    except IndexError:
        pass
    finally:
        qlock[pid].release()

def request_parser(message):
    global locks

    pid = None
    #handler = pchandler

    try:
        enc = message
        # send the config when the user joins
        if "NU" in enc:
            # get the user id
            new_user_list = enc["NU"]
            if len(new_user_list) != 1:
                raise Exception('Not supporting multiple new users')
            userid = new_user_list[0]

            if not game_loading and enc["SS"] == rcat.pc.adm_id:
                jigsaw.send_game_to_clients([userid])

        elif "UD" in enc:
            # User was disconnected. Inform other clients
            if enc["SS"] == rcat.pc.adm_id:
                del enc["SS"]
                datacon.mapper.disconnect_user(enc["UD"])
                if enc["UD"] in locks:
                    # TODO: this does not seem to be called, cf issue #98
                    pid = locks[enc["UD"]]
                    piece = datacon.mapper.get_piece(pid)
                    response = {'M': {'pd': {'id': piece.pid, 'x':piece.x, 'y':piece.y, 'b':piece.b}}}  #  no 'U' = broadcast
                    jsonmsg = json.dumps(response)
                    IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
                    # Cleanup database
                    suc = datacon.mapper.drop_piece(pid, {'l':None, 'x':piece.x, 'y':piece.y})
                    if not suc:
                        logging.warning("[jigsawapp]: Could not release piece after disconnection.")
                
                response = {'M': enc}
                jsonmsg = json.dumps(response)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))

        else:
            if game_loading:
                msg = {'M': {'go':True}}
                jsonmsg = json.dumps(msg)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
                return

            # usual message
            logging.debug(enc["M"])
            m = json.loads(enc["M"])
            userid = enc["U"][0]

            if 'usr' in m:  # 1- client connects to server/opens websocket
                # 2- server sends game state: 'c' config msg
                # 3- client answers with its user name: 'usr' msg
                # 4- server tells all clients about the new user: 'NU' msg 
                update_res = datacon.mapper.new_user_connected(userid, m['usr'])
                if m['usr'] != 'Guest':  # guests don't have points
                    response = {'M': {'NU':update_res}}  # New User
                    jsonmsg = json.dumps(response)
                    IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))

            elif 'pm' in m:  # piece movement
                pid = m['pm']['id']
                x = m['pm']['x']
                y = m['pm']['y']
                
                success = datacon.mapper.lock_piece(pid,userid)
                if success:
                    locks[userid] = pid
                    # update piece coords
                    datacon.mapper.move_piece(pid, {'x':x, 'y':y})
                    
                    # add lock owner to msg to broadcast
                    response = {'M': {'pm': {'id': pid, 'x':x, 'y':y, 'l':userid}}}  #  no 'U' = broadcast
                    
                    # broadcast
                    jsonmsg = json.dumps(response)
                    IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
                    return pid
                
            elif 'pd' in m:  # piece drop
                pid = m['pd']['id']
                x = m['pd']['x']
                y = m['pd']['y']
                bound = m['pd']['b']

                piece = datacon.mapper.get_piece(pid)
                if not piece:
                    raise Exception("Could not retrieve piece for lock check.")
                if not piece.l:
                    logging.warning("[jigsawapp]: Dropped a piece that did not have a lock. %s" % piece)
                    return
                
                lockid = piece.l
                if lockid and lockid == userid and not piece.b:  # I was the owner
                    if userid in locks:
                        del locks[userid]

                    if bound:  # we know the piece is not bound yet
                        logging.debug('%s bound piece %s at %d,%d'
                                  % (userid, pid, x, y))
                        datacon.mapper.bind_piece(pid,{'l':None, 'x':x, 'y':y, 'b':1})

                        # Update score board. Separate from 'pd' message because this is always broadcasted.
                        update_res = datacon.mapper.add_to_user_score(userid)
                        # If not update_res means we have an anonymous user, no scores should be sent
                        if update_res:
                            response = {'M': {'scu':update_res}}
                            jsonmsg = json.dumps(response)
                            IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))

                    else:
                        # unlock piece and update piece coords
                        res = datacon.mapper.drop_piece(pid, {'l':None, 'x':x, 'y':y})
                        if res:
                            logging.debug('%s dropped piece %s at %d,%d'
                                      % (userid, pid, x, y))
                        else:
                            logging.error("[jigsaw]: Error dropping piece")
                else:
                    logging.debug("[jigsaw]: Never got the lock, how odd.")
                # add lock owner to msg to broadcast
                response = {'M': {'pd': {'id': pid, 'x':x, 'y':y, 'b':bound}}}  #  no 'U' = broadcast
                jsonmsg = json.dumps(response)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
                return pid
            elif 'ng' in m:
                pass

            elif 'rg' in m:
                pass

    except Exception:
        logging.exception("[jigsawapp]: Exception in message handling from client:")


class JigsawServer():
    def __init__(self):
        global workers
        global settings
        
        self.workers = ThreadPool(6)
        """
        START JigsawServer attributes:
        """
        workers = self.workers
        # Collection of locked pieces to user. Used for disconnection cases.
        self.piece_locks = piece_locks
        # Used to know about new users, servers, and their disconnections.
        self.datacon = datacon
        # Who is the coordinator/leader of Jigsaw? We'll find out soon..
        self.coordinator = False
        # Temporary state, when game is loaded and ready, game_loading becomes false
        self.game_loading = game_loading
        # Determines when the game ends. Useful only if leader
        self.game_over = False
        """
        END Attributes
        """ 
        # Hooks up to get messages coming in admin channel
        rcat.pc.set_admin_handler(self.admin_parser)
        config = helper.open_configuration('jigsaw.cfg')
        settings = self.jigsaw_parser(config)
        helper.close_configuration('jigsaw.cfg')

        user = settings["db"]["user"]
        password = settings["db"]["password"]
        address = settings["db"]["address"]
        database = settings["db"]["db"]

        # Open connection to database and setup pool of connections. Makes datacon.db calls possible.
        datacon.db.open_connections(address, user, password, database)
        
        if settings["main"]["start"] == "true":
            # Leader is responsible to clear out all previous host data, and register as the first node 
            # Other nodes will only start after the leader is done clearing and registering. This happens when leader sends NEW game.
            datacon.mapper.cleanup_obm()
            if "abandon" in settings["main"] and settings["main"]["abandon"] == "true":
                settings["main"]["abandon"] = True
            else:
                settings["main"]["abandon"] = False
            self.coordinator = True
            self.start_game()

    def check_game_end(self):
        global settings
        global game_loading
        global total_pieces 
        total_pieces = int(grid['nrows']) * int(grid['ncols'])
        
        while (not self.game_over):
            self.game_over = self.datacon.mapper.game_over(total_pieces)
            time.sleep(3)
                
        game_loading = True
        self.game_over = False
        scores = datacon.mapper.get_user_scores(20)  # top 20 and connected player scores
        msg = {'M': {'go':True, 'scores':scores}}
        jsonmsg = json.dumps(msg)
        IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
        settings["main"]["abandon"] = True

        self.start_game()

    # Used to end the game prematurely
    def declare_game_end(self):
        if self.coordinator == True:
            self.game_over = True
        else:
            if self.coordinator:
                msg = {"FW":{"RESTART":True},"ID":self.coordinator}
                jsonmsg = json.dumps(msg)
                self.send_app_message(jsonmsg)

    def send_game_to_clients(self, clients=None):
        # TODO: only send pieces in player's frustum
        pieces = []
        while(not pieces):
            pieces = datacon.mapper.select_all()
        scores = datacon.mapper.get_user_scores(20)  # top 20 and connected player scores

        # send the board config
        cfg = {'img':img_settings,
               'board': board,
               'grid': grid,
               'frus': dfrus,
               'pieces': pieces,
               'scores' : scores
               }
        
        if not clients:
            # Named clients
            for client in scores['connected']:
                cfg['myid'] = client['uid'] 
                response = {'M': {'c': cfg}, 'U': [client['uid']]}
                jsonmsg = json.dumps(response)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
                
            # Guest clients
            guests = datacon.mapper.get_guests()
            for client in guests:
                cfg['myid'] = client
                response = {'M': {'c': cfg}, 'U': [client]}
                jsonmsg = json.dumps(response)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))
        else:
            for client in clients:
                cfg['myid'] = client
                response = {'M': {'c': cfg}, 'U': [client]}
                jsonmsg = json.dumps(response)
                IOLoop.instance().add_callback(functools.partial(pchandler.sync_reply,jsonmsg))


    def jigsaw_parser(self, config):
        app_config = {"main":{"start":"false"}}

        if config:
            try:
                set_main = {}
                set_board = {}
                set_img = {}
                set_grid = {}
                set_db = {}
                for k, v in config.items('Jigsaw_Main'):
                    set_main[k] = v
                app_config["main"] = set_main
                for k, v in config.items('Jigsaw_DB'):
                    set_db[k] = v
                app_config["db"] = set_db
                for k, v in config.items('Jigsaw_Image'):
                    set_img[k] = v
                app_config["img"] = set_img
                for k, v in config.items('Jigsaw_Board'):
                    set_board[k] = float(v)
                app_config["board"] = set_board
                for k, v in config.items('Jigsaw_Grid'):
                    set_grid[k] = int(v)
                app_config["grid"] = set_grid

            except ConfigParser.NoSectionError:
                logging.warn("[jigsawpp]: No Section exception. Might be OK!")
        return app_config

    # Parses messages coming through admin channel of proxy
    def admin_parser(self, msg):
        global game_loading
        global settings
        if "BC" in msg:
            if "NEW" in msg["BC"]:
                global board
                global img_settings
                global grid
                game_loading = True
                terminal.pause_terminal()

                # Tell mapper to register cacheable objects with the OBM. Only fires once.
                datacon.mapper.init_obm([User,Piece])

                if "C" in msg["BC"]:
                    # If I am not already the coordinator, store who is 
                    if self.coordinator != True:
                        self.coordinator = msg["BC"]["C"]
                    #logging.info("[jigsawapp]: Coordinator is " + coordinator)

                newgame_settings = msg["BC"]["NEW"]
                board = newgame_settings["board"]
                img_settings = newgame_settings["img"]
                grid = newgame_settings["grid"]
                datacon.mapper.join(newgame_settings)
                # If I am the coordinator, start the game!
                if self.coordinator == True:
                    self.coordinator = True
                    logging.info("[jigsawapp]: Starting game, please wait...")
                    newgame = True
                    # Restarting the game at the user's command, or at game over
                    if settings["main"]["abandon"] == True:
                        logging.info("[jigsawapp]: Abandoning old game.")
                        datacon.mapper.reset_game(keep_users=True)
                        settings["main"]["abandon"] = False
                        newgame = False

                    count = datacon.db.count(Piece)
                    if count == grid["ncols"] * grid["nrows"]:
                            logging.info("[jigsawapp]: Recovering last game.")
                            datacon.mapper.recover_last_game()
                    else:
                        # Prepares the pieces in the database
                        # If newgame is False, means we already reset the game either due to restart or game over
                        if newgame:
                            datacon.mapper.reset_game()
                        pieces = []
                        for r in range(grid['nrows']):
                            for c in range(grid['ncols']):
                                pid = str(uuid.uuid4()) # piece id
                                b = 0  # bound == correctly placed, can't be moved anymore
                                l = None # lock = id of the player moving the piece
                                x = randint(0, board['w'] - grid['cellw'])
                                y = randint(0, board['h'] - grid['cellh'])
                                
                                pieces.append(Piece(pid, x, y, c, r, b, l))
                                
                        datacon.mapper.create_pieces(pieces)

                    # Game end checker
                    t = Thread(target=self.check_game_end)
                    t.daemon = True
                    t.start()

                    # Tell servers that new game started
                    newmsg = {"BC":{"LOADED":True}}
                    json_message = json.dumps(newmsg)
                    proxy_admin = random.choice(rcat.pc.admin_proxy.keys())
                    proxy_admin.send(json_message)

                    # Tell clients about new game
                    self.send_game_to_clients()
                else:
                    # If I am not the leader, I still should send the new game to the guests, since the leader does not know about them 
                    # TODO: HACK! Should be fixed at some point
                    guests = self.datacon.mapper.get_guests()
                    self.send_game_to_clients(guests)


            elif "LOADED" in msg["BC"]:
                game_loading = False
                logging.info("[jigsawapp]: Game has loaded. Have fun!")
                terminal.show_terminal()
            
        elif "FW" in msg:
            if self.coordinator == True:
                if "RESTART" in msg["FW"]:
                    self.game_over = True

    def start_game(self):
        global settings
        # Tells all other servers to start game and gives a fixed list of admins so that they all create the same Data Structure
        mod_settings = deepcopy(settings)
        del mod_settings["main"]["start"]
        mod_settings["ADMS"] = list(rcat.pc.admins)
        newmsg = {"BC":{"NEW":mod_settings, "C":rcat.pc.adm_id}}
        json_message = json.dumps(newmsg)
        #proxy_admin = self.pick_random_proxy()
        #proxy_admin.send(json_message)
        self.send_app_message(json_message)
        
    def send_app_message(self,message):
        proxy_admin = random.choice(rcat.pc.admin_proxy.keys())
        proxy_admin.send(message)
        

def start():
    global rcat
    global datacon
    global jigsaw
    global terminal
    logging.config.fileConfig("connector_logging.conf")
    rcat = RCAT(JigsawServerHandler, SQLAlchemyConnector, JigsawMapper, ObjectManager)
    datacon = rcat.datacon
    logging.debug('[jigsawapp]: Starting jigsaw..')

    time.sleep(2)

    jigsaw = JigsawServer()
    jigsaw._debug = lambda cmd: eval(cmd)
    terminal = helper.Terminal(jigsaw)
    terminal.run_terminal()

if __name__ == "__main__":
    start()
    # To run profiler, uncomment next line
    # cProfile.run('start()', 'jigsaw.bench'i)
