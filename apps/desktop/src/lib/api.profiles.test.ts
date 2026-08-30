// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  gateway,
  getActiveProviderProfileId,
  profileCredentialName,
  saveProfileApiKey,
  setActiveProviderProfileId,
} from "./api";

const SECRET = "sk-frontend-secret-do-not-leak";

function responseJson(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const profileView = {
  id: "prof_1",
  name: "百炼-测试",
  provider: "bailian",
  credential_ref: "wincred:memecho:profile:prof_1:api_key",
  text_base_url: "",
  text_model: "",
  audio_base_url: "",
  realtime_ws_url: "",
  realtime_model: "",
  workspace_id: "",
  capabilities: [
    "realtime_asr",
    "file_transcription",
    "diarization",
    "audio_emotion",
    "text_analysis",
  ],
  created_at: "2026-08-29T00:00:00Z",
  updated_at: "2026-08-29T00:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("provider profile APIs", () => {
  it("never serializes an API key into profile create/update bodies", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson(profileView, 201))
      .mockResolvedValueOnce(responseJson(profileView));
    vi.stubGlobal("fetch", fetchMock);

    await gateway.createProfile({ name: "百炼-测试", provider: "bailian" });
    const credentialRef = await saveProfileApiKey("prof_1", SECRET);
    await gateway.updateProfile("prof_1", { credential_ref: credentialRef });

    const createBody = fetchMock.mock.calls[0][1].body as string;
    const updateBody = fetchMock.mock.calls[1][1].body as string;
    expect(createBody).not.toContain(SECRET);
    expect(updateBody).not.toContain(SECRET);
    expect(createBody).not.toContain("api_key");
    expect(JSON.parse(updateBody)).toEqual({
      credential_ref: "wincred:memecho:profile:prof_1:api_key",
    });
    expect(credentialRef).toBe(`wincred:memecho:${profileCredentialName("prof_1")}`);
  });

  it("binds sessions to the selected profile via snake_case contract", async () => {
    const fetchMock = vi.fn((_url: string, _init: RequestInit) =>
      Promise.resolve(
        responseJson({ id: "session-1", request_id: "request-1", status: "queued" }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await gateway.createSession("标题", "live", "prof_1");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toMatchObject({
      provider_profile_id: "prof_1",
    });

    await gateway.createSession("标题", "live");
    const unbound = JSON.parse(fetchMock.mock.calls[1][1].body as string);
    expect(unbound).not.toHaveProperty("provider_profile_id");
  });

  it("lists, verifies, and deletes profiles through the BYOK endpoints", async () => {
    const verification = {
      profile_id: "prof_1",
      ok: true,
      error_code: null,
      capabilities: [
        { capability: "text_analysis", status: "ok", error_code: null },
      ],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({ profiles: [profileView] }))
      .mockResolvedValueOnce(responseJson(verification))
      .mockResolvedValueOnce(responseJson({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(gateway.listProfiles()).resolves.toEqual([profileView]);
    await expect(gateway.verifyProfile("prof_1")).resolves.toMatchObject({ ok: true });
    await expect(gateway.deleteProfile("prof/1")).resolves.toEqual({ ok: true });

    expect(fetchMock.mock.calls[0][0]).toContain("/v1/provider-profiles");
    expect(fetchMock.mock.calls[1][0]).toContain("/v1/provider-profiles/prof_1/verify");
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
    expect(fetchMock.mock.calls[2][0]).toContain("/v1/provider-profiles/prof%2F1");
    expect(fetchMock.mock.calls[2][1].method).toBe("DELETE");
  });

  it("reports and reloads the editable profile configuration file", async () => {
    const status = {
      path: "C:\\Users\\demo\\AppData\\Roaming\\memEcho\\sessions\\gateway\\provider_profiles.json",
      profiles: 1,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson(status))
      .mockResolvedValueOnce(responseJson(status));
    vi.stubGlobal("fetch", fetchMock);

    await expect(gateway.profileConfigStatus()).resolves.toEqual(status);
    await expect(gateway.reloadProfileConfig()).resolves.toEqual(status);
    expect(fetchMock.mock.calls[0][0]).toContain("/v1/provider-profiles/config");
    expect(fetchMock.mock.calls[1][0]).toContain("/v1/provider-profiles/config/reload");
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
  });
});

describe("active provider profile selection", () => {
  it("persists the active profile locally and clears it when unset", () => {
    expect(getActiveProviderProfileId()).toBe("");
    setActiveProviderProfileId("prof_1");
    expect(getActiveProviderProfileId()).toBe("prof_1");
    setActiveProviderProfileId("");
    expect(getActiveProviderProfileId()).toBe("");
  });
});
