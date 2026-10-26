import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { request } from './http'

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

it('preserves request options and headers, parses JSON, and handles an empty 204', async () => {
  const signal = new AbortController().signal
  const body = JSON.stringify({ title: 'Write tests' })
  const emptyResponse = new Response(null, { status: 204 })
  const emptyJson = vi.spyOn(emptyResponse, 'json')
  fetchMock
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ id: 7 }), { status: 201 }),
    )
    .mockResolvedValueOnce(emptyResponse)

  await expect(
    request<{ id: number }>('/todos', {
      method: 'POST',
      body,
      signal,
      headers: {
        'Content-Type': 'application/custom+json',
        'X-Request-ID': 'unit-test',
      },
    }),
  ).resolves.toEqual({ id: 7 })
  expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/todos', {
    method: 'POST',
    body,
    signal,
    headers: {
      'Content-Type': 'application/custom+json',
      'X-Request-ID': 'unit-test',
    },
  })

  await expect(
    request<void>('/todos/7', { method: 'DELETE' }),
  ).resolves.toBeUndefined()
  expect(emptyJson).not.toHaveBeenCalled()
  expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/todos/7', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  })
})

it('maps validation, server detail, and unreadable or structured errors to useful messages', async () => {
  const cases = [
    {
      response: new Response(
        JSON.stringify({ detail: 'Internal validation data' }),
        { status: 422 },
      ),
      message:
        'Check the task details: title up to 120 characters, description up to 2000 characters, and a valid due date.',
    },
    {
      response: new Response(
        JSON.stringify({ detail: 'Task no longer exists' }),
        { status: 404 },
      ),
      message: 'Task no longer exists',
    },
    {
      response: new Response('Service unavailable', { status: 503 }),
      message: 'The operation failed. Please try again.',
    },
    {
      response: new Response(
        JSON.stringify({ detail: [{ message: 'Private diagnostic' }] }),
        { status: 500 },
      ),
      message: 'The operation failed. Please try again.',
    },
  ]

  for (const { response, message } of cases) {
    fetchMock.mockResolvedValueOnce(response)
    await expect(request('/todos')).rejects.toEqual(new Error(message))
  }
  expect(fetchMock).toHaveBeenCalledTimes(cases.length)
})

it('turns a network rejection into a connection message without leaking its details', async () => {
  fetchMock.mockRejectedValueOnce(new TypeError('Private network diagnostic'))

  await expect(request('/todos')).rejects.toEqual(
    new Error('Cannot connect to the server. Please try again.'),
  )
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('preserves cancellation instead of reporting a connection failure', async () => {
  const controller = new AbortController()
  controller.abort()
  const aborted = new DOMException('Request cancelled', 'AbortError')
  fetchMock.mockRejectedValueOnce(aborted)

  await expect(request('/todos', { signal: controller.signal })).rejects.toBe(
    aborted,
  )
  expect(fetchMock).toHaveBeenCalledWith('/api/todos', {
    signal: controller.signal,
    headers: { 'Content-Type': 'application/json' },
  })
})
