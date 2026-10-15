import { beforeEach, describe, expect, it, vi } from "vitest";
const WS="11111111-1111-4111-8111-111111111111";
const captured: Array<{path:string;init?:Record<string,unknown>}> = [];
let upstream: unknown;
vi.mock("@/lib/session",()=>({getSessionToken:async()=>"synthetic-token",isWorkspaceId:()=>true}));
vi.mock("@/lib/target-api",()=>({targetFetch:async(path:string,_token:string,init?:Record<string,unknown>)=>{captured.push({path,init});return upstream;}}));
const {PUT}=await import("./route");
const call=(body:unknown)=>PUT(new Request(`https://app.example.test/api/workspaces/${WS}/category-mix`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}) as Parameters<typeof PUT>[0],{params:Promise.resolve({id:WS})});
beforeEach(()=>{captured.length=0;upstream={ok:true,data:{protocol_version:2,revision:9,mode:"equal",rows:[],conversion:{state:"complete"},destinations:[]}};});
describe("versioned mix forwarding",()=>{
 it("forwards reset action and complete response",async()=>{const body={protocol_version:2,action:"reset_equal",expected_revision:8};const response=await call(body);expect(JSON.parse(captured[0].init!.body as string)).toEqual(body);expect(await response.json()).toEqual((upstream as {data:unknown}).data);});
 it("preserves conflicts and their revision",async()=>{upstream={ok:false,status:409,error:"mix_conflict",metadata:{current_revision:9}};const response=await call({protocol_version:2,action:"save_custom",expected_revision:8,rows:[]});expect(response.status).toBe(409);expect(await response.json()).toEqual({error:"mix_conflict",current_revision:9});});
});
