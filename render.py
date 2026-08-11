services:
  - type: web
    name: vadrifts
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: python main.py
    disk:
      name: vadrifts-data
      mountPath: /data
      sizeGB: 1
