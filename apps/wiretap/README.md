# 🕵️‍♂️ Wiretap: Automated WAF-Bypassing Data Extractors

Wiretap is a suite of containerized Python scrapers designed to bypass enterprise Web Application Firewalls (WAFs) like Cloudflare Turnstile and DataDome. It executes automated daily intelligence gathering for specific markets and broadcasts high-value outliers to Discord webhooks.

Currently, Wiretap runs two primary operations:
1.  **🌿 Terpene Hunter:** Scans localized dispensary menus (Dutchie, iHeartJane) to identify optimal medicinal profiles (High Myrcene / High THC) and filters out ghost inventory.
2.  **🎧 EDM Hunter:** Scans event aggregators (EDMTrain, Resident Advisor) to detect newly announced underground and commercial electronic music shows for targeted artists.

---

## 🏗️ Architecture & WAF Bypass Strategy

Modern enterprise firewalls have evolved beyond header-checking and TLS fingerprinting. Platforms like Cloudflare Turnstile now execute geometry checks to verify if the browser is running with a physical monitor attached. Standard headless browsers (Playwright, Puppeteer) are instantly shadow-banned.

**The Wiretap Bypass:**
* **Engine:** `SeleniumBase` running in Undetected Chromedriver (UC) mode.
* **Virtual Display:** Runs a fully graphical, non-headless instance of Google Chrome inside a Linux X11 virtual framebuffer (`Xvfb`). This satisfies all geometry and rendering checks, convincing the WAF that the request originates from a genuine user on a desktop monitor.
* **Container Optimization:** Base image utilizes `python:3.11-slim` with a manual installation of `google-chrome-stable` and Debian 13 secure keyrings.

---

## 🚀 Infrastructure & Deployment

Wiretap is deployed declaratively to a K3s (Lightweight Kubernetes) cluster via **ArgoCD** following strict GitOps protocols. 

### Critical Configuration: The Shared Memory Fix
By default, Docker and Kubernetes allocate a microscopic `64MB` to the container's `/dev/shm` (Shared Memory) partition. A full graphical Chrome browser requires significantly more memory to render complex DOMs and solve CAPTCHAs. 

To prevent instant `CrashLoopBackOff` or `OOMKilled` errors, the Kubernetes CronJob manifests explicitly map the node's RAM to the container's shared memory:
```yaml
volumeMounts:
  - name: dshm
    mountPath: /dev/shm
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 1Gi

📁 Directory Structure
Plaintext

apps/wiretap/
├── docker/
│   ├── Dockerfile
│   ├── edm_hunter.py
│   └── terpene_hunter.py
└── manifests/
    ├── edm-cronjob.yaml      # Schedules execution every 4 hours
    └── terpene-cronjob.yaml  # Schedules execution at 09:00 and 21:00

⚙️ Environment Variables & Secrets

Wiretap relies on Kubernetes Secrets to securely inject Discord webhook URLs into the pods at runtime.

Required Keys:

    DISCORD_WEBHOOK_TERPENE: Webhook URL for the dispensary alerts.

    DISCORD_WEBHOOK_EDM: Webhook URL for the concert alerts.

Applying the Secret (Manual Pre-requisite):
Before ArgoCD can successfully synchronize the CronJobs, the secret must exist in the wiretap namespace:
Bash

kubectl create namespace wiretap

kubectl create secret generic wiretap-secrets \
  --from-literal=DISCORD_WEBHOOK_TERPENE='[https://discord.com/api/webhooks/](https://discord.com/api/webhooks/)...' \
  --from-literal=DISCORD_WEBHOOK_EDM='[https://discord.com/api/webhooks/](https://discord.com/api/webhooks/)...' \
  -n wiretap

🛠️ Local Development & Building

If you are updating the Python extraction logic, you must rebuild the Docker image and push it to the cluster's local containerd registry before committing the updated manifest to Git.

Build Command (On K3s Worker Node):
Bash

sudo docker build -t wiretap:v14.0-uc . && \
sudo docker save wiretap:v14.0-uc -o wiretap_v14.0.tar && \
sudo k3s ctr images import wiretap_v14.0.tar && \
rm wiretap_v14.0.tar && sudo docker system prune -f

Note: Remember to update the image: tag in the manifests/*.yaml files before pushing to GitHub to trigger the ArgoCD synchronization.
🚨 Troubleshooting
Error	Cause	Resolution
CrashLoopBackOff (No Logs)	Shared memory exhaustion.	Ensure the dshm emptyDir volume is correctly mounted in the CronJob manifest.
0 valid profiles extracted	WAF successfully blocked the graphical browser.	Cloudflare may have updated its heuristics. Verify that sb.uc_open_with_reconnect() is being used instead of a standard sb.open().
ErrImageNeverPull	K3s cannot find the specified image tag.	Verify you ran the k3s ctr images import command on the specific worker node scheduled to run the pod.
