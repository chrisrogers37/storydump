"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  LINK_TTL_MINUTES_FALLBACK,
  requestTelegramGroupLink,
  requestTelegramLink,
  startCommandFor,
  telegramGroupLinkRefusalCopy,
  telegramLinkRefusalCopy,
} from "@/lib/telegram-link";
import type { ChannelBinding } from "@/lib/types";

/**
 * Telegram — the IDENTITY link and the GROUP links, on one card.
 *
 * The two halves look alike and are not the same thing. The identity link is
 * about the PERSON (#1172 clause 1): it attaches a Telegram account to a
 * Storydump user, once, across every workspace. The group link is about THIS
 * WORKSPACE (`07` §13): it binds a chat that this workspace's approval cards
 * go to. That is why they mint through different functions, say different
 * sentences, and why only the group half has an error slot of its own.
 *
 * ── Why these stay two blocks and not one component ──────────────────────
 *
 * #1216 considered folding them into a shared `OneShotLinkBlock`. What
 * actually differs between them is the anchor label, the resting button's
 * label AND its class, the wrapper's class, the whole explanatory paragraph
 * (the group's carries a `<code>` with the fallback `/start` command), the
 * error slot, and where the TTL sentence goes — the identity half states it
 * up front, before any link exists, and the group half states it inside the
 * explanation afterwards. Seven props is the duplication again with
 * indirection in front of it, so they are left as they read.
 */
export function TelegramCard({
  workspaceId,
  telegramLinked,
  telegramDisplayName,
  bindings,
  onError,
  onNotice,
}: {
  workspaceId: string;
  /** Whether the signed-in USER has a Telegram identity attached — a fact
   *  about the person, not this workspace (#1172 clause 1). */
  telegramLinked: boolean;
  /** Who that identity is, so a link tapped by the wrong person is visible. */
  telegramDisplayName: string | null;
  /** The Telegram chats this WORKSPACE's cards go to (`07` §13); null = could not be loaded. */
  bindings: ChannelBinding[] | null;
  /** The tab owns the banner both cards speak through. */
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
}) {
  const [telegramLink, setTelegramLink] = useState<{
    link: string;
    expiresInSeconds: number;
  } | null>(null);
  const [linkingTelegram, setLinkingTelegram] = useState(false);
  const [groupLink, setGroupLink] = useState<{
    link: string;
    expiresInSeconds: number;
  } | null>(null);
  const [mintingGroupLink, setMintingGroupLink] = useState(false);
  const [groupLinkError, setGroupLinkError] = useState<string | null>(null);
  const boundGroups = (bindings ?? []).filter((b) => b.state === "active");

  /**
   * Mint the Telegram deep link and SHOW it rather than navigate: the tap has
   * to happen inside Telegram, on whatever device the person has it on, so a
   * same-tab `location.assign` to `t.me` would strand a desktop browser on
   * Telegram's web landing page. An anchor opens it where Telegram is
   * installed; the raw link is there to copy to a phone.
   */
  async function linkTelegram() {
    onError(null);
    onNotice(null);
    setLinkingTelegram(true);
    const result = await requestTelegramLink();
    setLinkingTelegram(false);
    if (!result.ok) {
      onError(telegramLinkRefusalCopy(result.error));
      return;
    }
    setTelegramLink({
      link: result.link,
      expiresInSeconds: result.expiresInSeconds,
    });
  }

  /** Mint the group-picker link (`07` §13) and SHOW it, like the identity link. */
  async function addTelegramGroup() {
    setGroupLinkError(null);
    setMintingGroupLink(true);
    const result = await requestTelegramGroupLink(workspaceId);
    setMintingGroupLink(false);
    if (!result.ok) {
      setGroupLinkError(telegramGroupLinkRefusalCopy(result.error));
      return;
    }
    setGroupLink({
      link: result.link,
      expiresInSeconds: result.expiresInSeconds,
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Telegram</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {telegramLinked ? (
          <div className="flex items-center gap-2">
            <Badge variant="secondary" className="bg-green-100 text-green-800">
              Linked
            </Badge>
            <p className="text-sm text-muted-foreground">
              {telegramDisplayName
                ? `Telegram account "${telegramDisplayName}" is linked to your Storydump account.`
                : "A Telegram account is linked to your Storydump account."}{" "}
              If that is not you, contact us — there is no unlink control yet.
            </p>
          </div>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Link your Telegram account to approve posts and receive
              notifications there. Linking is per person, not per workspace, and
              the link below works once and expires after{" "}
              {telegramLink?.expiresInSeconds
                ? Math.round(telegramLink.expiresInSeconds / 60)
                : LINK_TTL_MINUTES_FALLBACK}{" "}
              minutes.
            </p>
            {telegramLink ? (
              <div className="space-y-2">
                <Button asChild>
                  <a
                    href={telegramLink.link}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open Telegram to finish linking
                  </a>
                </Button>
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {telegramLink.link}
                </p>
                <p className="text-xs text-muted-foreground">
                  <strong>Do not share this link.</strong> Whoever taps it links
                  their Telegram to your account. Tap Start in the chat that
                  opens — the bot confirms in the chat — then reload this page;
                  it shows Linked once the bot has heard from you. Asking for a
                  new link retires this one.
                </p>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={linkTelegram}
                  disabled={linkingTelegram}
                >
                  {linkingTelegram ? "Preparing link..." : "Get a new link"}
                </Button>
              </div>
            ) : (
              <Button
                variant="outline"
                onClick={linkTelegram}
                disabled={linkingTelegram}
              >
                {linkingTelegram ? "Preparing link..." : "Link Telegram"}
              </Button>
            )}
          </>
        )}
        <div className="border-t pt-3">
          <p className="text-sm font-medium">Telegram groups</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Approval cards and notices for this workspace go to every group
            listed here. Adding one opens Telegram&apos;s group picker; the
            group you choose is bound to this workspace. A group can belong to
            one workspace only.
          </p>
          {bindings === null ? (
            <p className="mt-2 text-sm text-muted-foreground">
              Bound groups could not be loaded just now. Reload to try again.
            </p>
          ) : boundGroups.length > 0 ? (
            <ul className="mt-2 space-y-1 text-sm">
              {boundGroups.map((b) => (
                <li key={b.id} className="flex items-center gap-2">
                  <Badge
                    variant="secondary"
                    className="bg-green-100 text-green-800"
                  >
                    Bound
                  </Badge>
                  <span className="text-muted-foreground">
                    {b.channel === "telegram_dm" ? "Direct chat" : "Group chat"}{" "}
                    · id {b.external_ref}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground">
              No Telegram group is bound yet.
            </p>
          )}
          {groupLinkError && (
            <p className="mt-2 text-sm text-red-700">{groupLinkError}</p>
          )}
          {groupLink ? (
            <div className="mt-3 space-y-2">
              <Button asChild>
                <a
                  href={groupLink.link}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open Telegram to choose a group
                </a>
              </Button>
              <p className="break-all font-mono text-xs text-muted-foreground">
                {groupLink.link}
              </p>
              <p className="text-xs text-muted-foreground">
                Only you can use this link, from the Telegram account linked to
                your Storydump user. It works once and expires after{" "}
                {Math.round(groupLink.expiresInSeconds / 60)} minutes; the bot
                confirms in the group, then reload this page. If the bot is
                already in the group and nothing arrives, send this in the group
                instead:{" "}
                <code className="break-all">
                  {startCommandFor(groupLink.link)}
                </code>
              </p>
              <Button
                variant="ghost"
                size="sm"
                onClick={addTelegramGroup}
                disabled={mintingGroupLink}
              >
                {mintingGroupLink ? "Preparing link..." : "Get a new link"}
              </Button>
            </div>
          ) : (
            <Button
              variant="outline"
              className="mt-3"
              onClick={addTelegramGroup}
              disabled={mintingGroupLink}
            >
              {mintingGroupLink ? "Preparing link..." : "Add a Telegram group"}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
