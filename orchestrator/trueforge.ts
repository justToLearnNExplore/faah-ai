import { TrueForge, TrueForgeApi } from "@truefoundry/trueforge-sdk";
import { config } from "./config.js";

export type { TrueForgeApi };

export const tf = new TrueForge({ baseUrl: config.trueforgeUrl, token: config.trueforgeApiKey });

export async function trueforgeUp(): Promise<boolean> {
  try {
    await tf.server.getCapabilities({ timeoutInSeconds: 3, maxRetries: 0 });
    return true;
  } catch {
    return false;
  }
}
