# marathon-cloudxns
Script to update skydns based on mesos and marathon state.

You can run the script directly or using the Docker image.

See [comments in script](marathon-cloudxns.py) for more details.

# Docker
Synopsis: `docker run index.xxxxx.com/marathon-cloudxns event|poll ...`

## sse mode
In SSE mode, the script connects to the marathon events endpoint to get
notified about state changes.

Syntax: `docker run index.xxxxx.com/marathon-cloudxns sse [other args]`

## event mode
In event mode, the script registers a http callback in marathon to get
notified when state changes.

Syntax: `docker run index.xxxxx.com/marathon-cloudxns event callback-addr:port [other args]`

## poll mode
If you can't use the http callbacks, the script can poll the APIs to get
the schedulers state periodically.

Synatax: `docker run index.xxxxx.com/marathon-cloudxns poll [other args]`

To change the poll interval (defaults to 60s), you can set the POLL_INTERVALL
environment variable.
