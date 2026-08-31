export type ProviderState =
  | 'ready'
  | 'login_required'
  | 'unsupported'
  | 'confinement_failed'
  | 'unavailable'

export type StatusPayload = {
  product: { name: 'Clipen'; workspace: 'local' }
  provider: { name: string; state: ProviderState; message: string }
  session: { csrf_token: string }
}

export type DirectoryRole = 'source' | 'reference'
export type DirectoryAvailability =
  | 'available'
  | 'offline'
  | 'changed'
  | 'permission_required'

export type DirectoryReference = {
  role: DirectoryRole
  display_path: string
  display_name: string
  availability: DirectoryAvailability
}

export type Project = {
  project_id: string
  name: string
  brief: string
  stage: 'materials'
  revision: number
  created_at: string
  updated_at: string
  directories: DirectoryReference[]
}

export type DirectorySelection = {
  selection_token: string
  role: DirectoryRole
  display_path: string
  display_name: string
  expires_in_seconds: number
}

export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
  })
  if (response.status === 204) return null as T
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new ApiError('response_invalid', 'Clipen 回傳了無法辨識的資料。', response.status)
  }
  if (!response.ok) {
    const error = payload as { error?: { code?: unknown; message?: unknown } }
    throw new ApiError(
      typeof error.error?.code === 'string' ? error.error.code : 'request_failed',
      typeof error.error?.message === 'string' ? error.error.message : 'Clipen 無法完成這次操作。',
      response.status,
    )
  }
  return payload as T
}

async function command<T>(path: string, csrf: string, body: object): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Clipen-CSRF': csrf,
    },
    body: JSON.stringify(body),
  })
}

export async function bootstrapSession(token: string): Promise<void> {
  await request<{ connected: true }>('/api/session/bootstrap', {
    method: 'POST',
    headers: { 'X-Clipen-Bootstrap': token },
  })
}

export async function getStatus(): Promise<StatusPayload> {
  return request<StatusPayload>('/api/status')
}

export async function listProjects(): Promise<Project[]> {
  return (await request<{ projects: Project[] }>('/api/projects')).projects
}

export async function getProject(projectId: string): Promise<Project> {
  return (await request<{ project: Project }>(`/api/projects/${projectId}`)).project
}

export async function createProject(csrf: string, name: string): Promise<Project> {
  return (
    await command<{ project: Project }>('/api/commands/create-project', csrf, {
      request_id: crypto.randomUUID(),
      name,
    })
  ).project
}

export async function updateProjectDetails(
  csrf: string,
  project: Project,
  name: string,
  brief: string,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/update-details`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        name,
        brief,
      },
    )
  ).project
}

export async function selectDirectory(
  csrf: string,
  role: DirectoryRole,
): Promise<DirectorySelection | null> {
  const result = await command<{ selection: DirectorySelection } | null>(
    '/api/system/commands/select-directory',
    csrf,
    { request_id: crypto.randomUUID(), role },
  )
  return result?.selection ?? null
}

export async function saveDirectory(
  csrf: string,
  project: Project,
  role: DirectoryRole,
  selectionToken: string,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/set-directory`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        role,
        selection_token: selectionToken,
      },
    )
  ).project
}

export async function removeDirectory(
  csrf: string,
  project: Project,
  role: DirectoryRole,
): Promise<Project> {
  return (
    await command<{ project: Project }>(
      `/api/projects/${project.project_id}/commands/remove-directory`,
      csrf,
      {
        request_id: crypto.randomUUID(),
        expected_revision: project.revision,
        role,
      },
    )
  ).project
}
