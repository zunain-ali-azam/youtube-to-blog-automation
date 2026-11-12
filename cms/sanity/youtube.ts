// The video embed yt2blog puts near the top of each post's body.
import {PlayIcon} from '@sanity/icons'
import {defineField, defineType} from 'sanity'

export const youtube = defineType({
  name: 'youtube',
  title: 'YouTube video',
  type: 'object',
  icon: PlayIcon,
  fields: [
    defineField({
      name: 'url',
      title: 'Video URL',
      type: 'url',
      validation: (rule) => rule.required(),
    }),
  ],
  preview: {
    select: {title: 'url'},
  },
})
