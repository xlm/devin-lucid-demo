# Illustrative Kubernetes manifests

These files are illustrative only. They have not been applied or tested
anywhere.

The legacy Deployment represents the active producer writing to the
charging summary topic. The candidate Deployment represents a shadow
producer writing to a separate shadow topic. A cutover would require a
human decision and an external coordination mechanism.

A real single-active-producer design could coordinate ownership with a
Kubernetes `Lease` and fence stale producers. This repository does not
implement that Lease mechanism. Its cutover simulator uses a local
`fcntl.flock` file and does not provide distributed coordination or atomic
handover.

The image names are placeholders. These manifests do not describe a
production deployment.
