# bbh-server
Server for Boxhead Bounty Hunter

NOTES: This server is currently local only (meaning it's essentially singleplayer lol) and runs on Python. To run the server locally, you need to:

1. Clone/Download the files
2. Move the "assets.swf" and "Constants.xml" files to a folder called "_assets" that is in the same directory that you downloaded the rest of the data (e.g. server2.py)
3. Install JPEXS Flash Player Projector and open the bbh.swf file. Navigare to boxhead.assets.AssetLoader and edit the ActionScript file to display the correct location of the assets.swf and constants.xml (lines 78, 128, and 140).
4. Install Python and dependencies if not already installed
5. In a terminal/PowerShell window, navigate to the directory containing the files and run "python server2.py"
6. open the bbh.swf file using Adobe Flash Player Projector.
7. Connect and enter a username and password on the login screen (NOT create account!). It will log you in and create the account you asked for. If you forget your password just delete the Users.db file and start again
8. Enjoy!
