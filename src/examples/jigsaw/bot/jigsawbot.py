'''
Created on Jul 30, 2012

@author: Arthur Valadares
'''

import websocket
import logging
import json
import threading
import time

class Bot():
    running = False
    def on_message(self,ws, message):
        msg = json.loads(message)
        if 'c' in msg:
            self.start_game(msg['c'])
            
        elif 'pm' in msg:
            id = msg['pm']['id']
            x = msg['pm']['x']
            y = msg['pm']['y']
            owner = msg['pm']['l']
            # TODO: finish
        elif 'pd' in msg:
            id = msg['pd']['id']
            x = msg['pd']['x']
            y = msg['pd']['y']
            owner = msg['pd']['l']
            # TODO: finish
    
    def on_error(self,ws, error):
        logging.exception("[jigsawbot]: Exception in Bot handler:")
    
    def on_close(self,ws):
        pass
    
    def on_open(self,ws):
        self.running = True
        threading.Thread(target=self.automate_bot,args=[ws])
    
    def start_game(self,cfg):
        self.imgurl = cfg['imgurl']
        self.board = cfg['board']
        self.grid = cfg['grid']
        self.dfrus = cfg['frus']
        self.pieces = cfg['pieces']
        self.myid = cfg['myid']
        
    def automate_bot(self,ws):
        x,y = 0,0
        while self.running:
            for piece in self.pieces:
                while y < self.board.h:
                    while x < self.board.w:
                        if not piece['l'] or piece['l'] == "None":
                            ws.send(self.move_piece(piece,x,y))
                            time.sleep(0.1)
                            x += 5
                    y += 5
                    

    def move_piece(self,p,x,y):
        msg = {'pm' : {'id':p.pid, 'x':x,'y':y}}
        jsonmsg = json.loads(msg)
        return jsonmsg

if __name__ == '__main__':
    websocket.enableTrace(True)
    bot = Bot()
    ws = websocket.WebSocketApp("ws://echo.websocket.org/",
                                on_message = bot.on_message,
                                on_error = bot.on_error,
                                on_close = bot.on_close)
    ws.on_open = bot.on_open
    ws.run_forever()