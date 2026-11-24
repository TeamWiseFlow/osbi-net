"""python -m wiseflow_server [--host 127.0.0.1] [--port 8078]."""
import argparse
import os


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description='Run the local material extraction server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=int(os.environ.get('WISEFLOW_SERVER_PORT', '8078')))
    args = parser.parse_args()
    uvicorn.run('wiseflow_server.app:app', host=args.host, port=args.port, workers=1)


if __name__ == '__main__':
    main()
