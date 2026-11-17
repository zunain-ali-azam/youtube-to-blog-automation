// The video embed yt2blog puts near the top of each post's body.
import type {Block} from 'payload'

export const YouTube: Block = {
  slug: 'youtube',
  interfaceName: 'YouTubeBlock',
  fields: [
    {
      name: 'url',
      label: 'Video URL',
      type: 'text',
      required: true,
    },
  ],
}
