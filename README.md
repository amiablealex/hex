# HexaStack

A turn-based hex tile sorting game for two players. Create a game, share the link, take turns.

## Quick Start (Development)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`

## Deploy on Raspberry Pi

```bash
# Clone/copy files to Pi
cd /home/pi/hexastack

# Set up venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Test it works
python app.py

# Install systemd service
sudo cp hexastack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hexastack
sudo systemctl start hexastack

# Check status
sudo systemctl status hexastack
```

## Cloudflare Tunnel

Add to your Cloudflare tunnel config (`~/.cloudflared/config.yml`):

```yaml
- hostname: hexastack.yourdomain.com
  service: http://localhost:5000
```

Then restart cloudflared:
```bash
sudo systemctl restart cloudflared
```

## Game Rules

- Two players share a hex board
- Each turn, pick 1 of 3 offered tile stacks and place it on an empty hex
- Tiles have 1-2 coloured layers
- Adjacent same-colour tops auto-merge
- Stack 10 same-colour layers to clear and score points
- Game ends when the board fills up or a player hits the target score
- Board sizes: Small (7 hexes, 3 colours), Medium (19 hexes, 4 colours), Large (37 hexes, 5 colours)
