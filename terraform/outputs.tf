output "aiops_namespace" {
  description = "Namespace where AIOps platform runs"
  value       = kubernetes_namespace.aiops.metadata[0].name
}

output "chaos_mesh_namespace" {
  description = "Namespace where Chaos Mesh operates"
  value       = kubernetes_namespace.chaos_mesh.metadata[0].name
}

