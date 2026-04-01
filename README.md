# bbh-server
Server for Boxhead Bounty Hunter

To run the server locally, you need to:

1. Clone/Download the files
2. Move the "assets.swf" and "Constants.xml" files to a folder called "_assets" that is in the same directory that you downloaded the rest of the data (e.g. server2.py)
3. Install JPEXS Flash Player Projector and open the bbh.swf file. Navigare to boxhead.assets.AssetLoader and edit the ActionScript file to display the correct location of the assets.swf and constants.xml (lines 78, 128, and 140).
4. Install Python and dependencies if not already installed
5. In a terminal/PowerShell window, navigate to the directory containing the files and run "python server2.py"
6. open the bbh.swf file using Adobe Flash Player Projector or Ruffle player.
7. Connect and enter a username and password on the login screen (NOT create account!). It will log you in and create the account you asked for. If you forget your password just delete the Users.db file and start again
8. Enjoy!

To run the server over the internet (so other players can play on the server), you need a host. I use DigitalOcean. They give you a virtual Linux machine that can be connected to. The lowest tier costs $4/month and handles BBH just fine. Upgraded tiers likely won't have any affect on performance because the things they upgrade aren't a problem at the lowest tier (e.g. the lowest tier only uses 5% total memory, so I don't need to upgrade server memory!) To start, buy the Linux droplet for $4/mo. Once you have the droplet, make sure to click "add a Reserved IP" and add an IPv4 address. This will be the server address you will use to connect.

To get the server working, use the following steps:

1. Open the SWF file in JPEXS Flash Decompiler. You need to edit the following lines:
     1. Lines 78, 128, and 131 of boxhead.assets.AssetLoader
     2. Line 102 of MMOcha.server.MMOchaserver
     3. Line 49 of MMOcha.server.DatabaseRequest
    
    For each of these, enter the server URL in the string. Make sure to click "save" for each individual file, then after all 3 have been modified, click "save as" under "file". Enter the name you want the swf to have. This is the file you will transfer to the server in step 5.
   
3. Click "Access", then "launch droplet console". Login as root. This may take a few tries. If you see "SSH Connection Lost", just keep trying. Eventuall you'll get in.
4. A terminal should display. You need to enter the following commands in succession:
  
   sudo apt update && sudo apt upgrade -y
   sudo apt install python3 python3-pip nginx -y
   sudo apt install python3 python3-pip nginx -y
   ufw allow 22/tcp
   ufw allow 80/tcp
   ufw allow 6123/tcp
   ufw enable
   mkdir ~/game_server
   cd ~/game_server

  Note that you may need to restart the server a few times. If you need to, just type "sudo reboot" and pick up where you left off.

5. Now you should be in the game server directory. You now need to transfer your files from your local machine to the server. The easiest way to do this is to use a program called FileZilla. When you have downloaded the program, connect to the server using the IP address and SFTP. Login as "root" and with the password you set on DigitalOcean. Drag the Python file to the game_server directory and the crossdomain.xml and swf file to /var/www/html. Then make a new folder in html called "_assets" and add the constants.xml file there. If the constants can't be found (e.g. guns don't fire), try making the file name lowercase.
6. Now entering the following commands:

   sudo chmod -R 755 /var/www/html/_assets/
   sudo apt install screen -y
   screen -S game
   cd ~/game_server
   sudo python3 bbh-server.py
   
8. Press Ctrl + A, then release both and press D.
9. Your server should now work! If it doesn't, try asking me (or an AI). You should be able to get it working eventually.
   
   
