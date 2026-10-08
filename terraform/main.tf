terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.26.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12.0"
    }
  }
}

provider "kubernetes" {
  config_path = var.kubeconfig_path
}

provider "helm" {
  kubernetes {
    config_path = var.kubeconfig_path
  }
}

resource "kubernetes_namespace" "aiops" {
  metadata {
    name = var.namespace
    labels = {
      environment = "production"
      platform    = "aiops-autonomous"
    }
  }
}

resource "kubernetes_namespace" "chaos_mesh" {
  metadata {
    name = "chaos-mesh"
  }
}

# Track T1's in-cluster backends live here: config.py's defaults are
# prometheus-k8s.monitoring.svc.cluster.local and
# loki-gateway.monitoring.svc.cluster.local, so the namespace has to exist
# before those DNS names can resolve.
resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
    labels = {
      "kubernetes.io/metadata.name" = "monitoring"
    }
  }
}

# Track T7: Chaos Mesh, installed the same way step 1 installs it by hand.
#
# The daemon's runtime and socket are variables because they belong to the
# *cluster*, not to this module: minikube here runs docker, a real cluster
# would run containerd at /run/containerd/containerd.sock. Pinning the chart
# version keeps `terraform plan` reproducible.
#
# create_namespace is false on purpose — kubernetes_namespace.chaos_mesh
# above owns that namespace, and letting the chart create it too would be
# two resources claiming the same object.
resource "helm_release" "chaos_mesh" {
  name       = "chaos-mesh"
  repository = var.chaos_mesh_repository
  chart      = "chaos-mesh"
  version    = var.chaos_mesh_version
  namespace  = kubernetes_namespace.chaos_mesh.metadata[0].name

  create_namespace = false
  atomic           = true
  timeout          = 600

  set {
    name  = "chaosDaemon.runtime"
    value = var.chaos_daemon_runtime
  }

  set {
    name  = "chaosDaemon.socketPath"
    value = var.chaos_daemon_socket_path
  }

  set {
    name  = "dashboard.create"
    value = "false"
  }

  depends_on = [kubernetes_namespace.chaos_mesh]
}

