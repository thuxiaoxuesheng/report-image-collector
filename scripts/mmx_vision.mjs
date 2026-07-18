import { stat } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { MiniMaxSDK } from "mmx-cli/sdk";

const PROMPT = `只判断整张图片是否属于医疗检查或检验报告单/报告结果页面。
report：能看到检查或检验项目、结果、参考范围，或影像、病理、内镜、功能检查的正式诊断/结论等报告结构；纸质、电子截图均可。
non_report：人体照片、聊天、就医攻略、缴费单、预约单、处方、检查申请单、费用清单、普通文字，或不含检查检验结果的图片。
不要抄录姓名、医院、就诊号、数值或任何医学内容，不判断结果是否正确。
只返回JSON：{"is_report":true或false,"confidence":0到1,"reason":"不含个人信息的简短判断理由"}`;

const key = process.env.MINIMAX_API_KEY;
const imagePath = process.argv[2] ? resolve(process.argv[2]) : "";
if (!key || !imagePath) {
  process.stderr.write("缺少MiniMax密钥或图片路径\n");
  process.exit(2);
}

const allowed = new Set([".jpg", ".jpeg", ".png", ".webp"]);
const info = await stat(imagePath);
if (!info.isFile() || !allowed.has(extname(imagePath).toLowerCase()) || info.size > 20 * 1024 * 1024) {
  process.stderr.write("图片格式无效或超过20MB\n");
  process.exit(2);
}

try {
  const region = process.env.MINIMAX_REGION === "global" ? "global" : "cn";
  const sdk = new MiniMaxSDK({ apiKey: key, region });
  const response = await sdk.vision.describe({ image: imagePath, prompt: PROMPT });
  const content = typeof response?.content === "string" ? response.content : JSON.stringify(response);
  process.stdout.write(content);
} catch (error) {
  const message = error instanceof Error ? error.message : "未知错误";
  process.stderr.write(`${message.slice(0, 500)}\n`);
  process.exit(1);
}
