🏛️ House of Leaves NOC | K3s GitOps Hub
This repository serves as the Source of Truth for my home-based Network Operations Center (NOC). It utilizes a GitOps workflow to manage a multi-node Proxmox/K3s cluster, automating the deployment of custom-built intelligence tools and infrastructure.

🛠️ Cluster Architecture
Orchestration: K3s (Lightweight Kubernetes)

GitOps Engine: ArgoCD (Pull-based deployment)

Hardware: 12U Mini-Rack featuring BMAX Proxmox Nodes & Orico CyberNAS

Registry: Private local Docker Registry for custom images

Ingress/Security: Cloudflare Tunnels & Kubernetes Secrets (Opaque)

🚀 Projects Overview
1. 🎯 Infra-Hunter (Job Scraper v12.0)
An automated intelligence engine that snipes infrastructure and DevOps job postings across various ATS platforms (Greenhouse, Lever, Remotive).

Logic: Python-based scraper with signature matching for Kubernetes/Cloud roles.

Database: CouchDB (NoSQL) for lead persistence.

Alerting: Real-time Discord integration via Webhooks.

Security: Uses K8s envFrom to inject secrets at runtime, ensuring no plaintext credentials exist in source control.

2. ♟️ K8s Chess Platform (In Progress)
A high-performance analysis platform orchestrating Stockfish and Leela Chess Zero engines.

Goal: Distributed engine analysis scaled via Kubernetes Jobs.

🔐 Security Architecture
I follow the Zero-Trust principle for public repositories:

Secrets Management: Sensitive data (API keys, DB passwords) is manually provisioned into the cluster namespaces.

Manifests: Only deployment logic, resource limits, and service definitions are committed here.

Namespacing: Workloads are isolated (e.g., hunter-ops) to prevent cross-contamination of services.

📈 Monitoring & Logs
Logs are streamed from the cluster to a custom Proxmox Bridge API and visualized on a React-based 3U touchscreen dashboard mounted on the physical rack.
