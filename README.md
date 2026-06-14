# 💎 PrismSRE

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.103.0+-009688.svg)
![Kubernetes](https://img.shields.io/badge/Kubernetes-Compatible-326CE5.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

> **The next-generation, AI-powered Site Reliability Engineer for your Kubernetes Clusters.**
<img width="1536" height="1024" alt="gpt-overview" src="https://github.com/user-attachments/assets/a7a051f3-6cda-40a1-bd65-d357960b72a5" />

PrismSRE is a production-grade Kubernetes troubleshooting system that acts as an autonomous AI agent. It seamlessly bridges the gap between raw cluster metrics/logs and actionable SRE insights. Powered by the **Google Agent Development Kit (ADK)**, **Model Context Protocol (MCP)**, and a beautiful **Glassmorphism Dashboard**, PrismSRE provides immediate, intelligent diagnostics for your Kubernetes workloads.

---

## ✨ Features

- **🧠 Autonomous Diagnostics:** Powered by Google's Gemini models, capable of analyzing `CrashLoopBackOff`, `OOMKilled`, and stuck rollouts.
- **🛡️ Secure by Design:** Employs the Model Context Protocol (FastMCP) to enforce strict read-only access to the Kubernetes cluster. The AI agent operates outside the direct execution context.
- **🎨 Glassmorphism UI:** A breathtaking, dependency-free, single-file HTML dashboard using Vanilla JS and Tailwind CSS.
- **⚡ Real-time Context Gathering:** Automatically fetches pod status, deployment definitions, and tail logs through MCP tools without requiring raw shell access.
- **☁️ Cloud Agnostic:** Compatible with GKE, K3s, Minikube, and standard Kubernetes distributions.

---

## 🏗️ Architecture

For a deep dive into the system design, security boundaries, and component interaction, please see the [Architecture Documentation](architecture.md).

---

## 🚀 Getting Started

### Prerequisites
- Python 3.11+
- A running Kubernetes cluster (GKE, K3s, Minikube, etc.)
- `kubectl` configured and authenticated to your cluster
- A Google Gemini API Key

### Local Development

1. **Clone the repository:**
   ```bash
   git clone https://github.com/barbaria888/PrismSRE.git
   cd PrismSRE
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   ```bash
   cp .env.example .env
   ```
   Add your `GOOGLE_API_KEY` to the `.env` file.

4. **Run the Dashboard Server:**
   ```bash
   uvicorn app:app --reload --host 0.0.0.0 --port 8000
   ```
   Navigate to `http://localhost:8000` in your browser.

---

## ☸️ Running in Your Own Cluster

To deploy PrismSRE as a long-running service inside your Kubernetes cluster, follow these steps.

### 1. Create the Secret
The agent requires your Gemini API key to operate. We provide a compatible secret manifest.
Edit `secret.yaml` with your actual base64/plaintext key, then apply:
```bash
kubectl apply -f secret.yaml
```

### 2. Containerize the Application
Build and push the Docker image to your container registry:
```bash
# Example Dockerfile included in the project or write a simple one for FastAPI
docker build -t your-registry/prismsre:latest .
docker push your-registry/prismsre:latest
```

### 3. Deploy to Kubernetes
You can deploy the application using standard Kubernetes manifests. Ensure you grant the necessary RBAC permissions (read-only access to Pods, Deployments, and Logs).

```yaml
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: prismsre-sa
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: prismsre-reader
rules:
- apiGroups: ["", "apps"]
  resources: ["pods", "pods/log", "deployments", "events"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prismsre-reader-binding
subjects:
- kind: ServiceAccount
  name: prismsre-sa
  namespace: default
roleRef:
  kind: ClusterRole
  name: prismsre-reader
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prismsre
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: prismsre
  template:
    metadata:
      labels:
        app: prismsre
    spec:
      serviceAccountName: prismsre-sa
      containers:
      - name: prismsre
        image: your-registry/prismsre:latest
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: kubeops-ai-secret
---
apiVersion: v1
kind: Service
metadata:
  name: prismsre-service
spec:
  type: ClusterIP
  selector:
    app: prismsre
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
```
Apply the deployment:
```bash
kubectl apply -f deployment.yaml
```

*(Note: If you want external access, configure an Ingress or change the Service type to LoadBalancer).*

---

## 🛡️ Security Considerations

- **No Root Access:** The agent operates strictly with `ClusterRole` read-only permissions.
- **No Direct Shell:** Uses the Model Context Protocol to execute predefined tools, preventing Prompt Injection attacks that try to execute arbitrary bash commands.

---

## 📄 License

This project is licensed under the MIT License.
